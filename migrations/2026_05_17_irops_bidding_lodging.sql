-- ─────────────────────────────────────────────────────────────────────
-- Sprint 5 — IROPS, Crew Bidding, Lodging, Transport
--
-- Five additions in one file because they share the same migration
-- window. Each block is independent — feel free to apply only the
-- parts you need if you're back-porting.
-- ─────────────────────────────────────────────────────────────────────

-- ── 1. Crew seniority ──────────────────────────────────────────────
-- Date the crew member joined the airline. Drives bidding priority
-- (older seniority_date wins ties).
ALTER TABLE crew
    ADD COLUMN IF NOT EXISTS seniority_date DATE,
    ADD COLUMN IF NOT EXISTS hire_date      DATE;

CREATE INDEX IF NOT EXISTS idx_crew_seniority
    ON crew(seniority_date)
    WHERE seniority_date IS NOT NULL;


-- ── 2. Crew bids ───────────────────────────────────────────────────
-- One bid per crew per month. Stores preferences as a JSON blob so we
-- can grow the schema without further migrations.
--
-- preferences example:
--   {
--     "off_days":         ["2026-06-05","2026-06-12","2026-06-19"],
--     "preferred_routes": ["BGW-DXB", "BGW-AMM"],
--     "avoid_routes":     ["BGW-FRA"],
--     "partner_crew_ids": ["abc","def"],
--     "max_block_hours":  85,
--     "notes":            "..."
--   }
CREATE TABLE IF NOT EXISTS crew_bids (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    crew_id         TEXT REFERENCES crew(id) ON DELETE CASCADE,
    month           TEXT NOT NULL,           -- 'YYYY-MM'
    preferences     JSONB NOT NULL DEFAULT '{}'::jsonb,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked          BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE once roster published
    locked_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (crew_id, month)
);
CREATE INDEX IF NOT EXISTS idx_bids_company_month ON crew_bids(company_id, month);


-- ── 3. Lodging — hotels per station ────────────────────────────────
-- The catalogue of hotels we use at outstations. Allocations link
-- crew_lodging_assignment rows (next table) here.
CREATE TABLE IF NOT EXISTS crew_lodging (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    station_code    TEXT NOT NULL,           -- e.g. 'DXB','AMM','FRA'
    hotel_name      TEXT NOT NULL,
    hotel_address   TEXT,
    phone           TEXT,
    distance_min    INT,                     -- minutes from airport
    rating          NUMERIC(3,1),
    notes           TEXT,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lodging_station ON crew_lodging(station_code);


-- Specific assignment of a crew member to a lodging during a layover.
CREATE TABLE IF NOT EXISTS crew_lodging_assignment (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    crew_id         TEXT REFERENCES crew(id) ON DELETE CASCADE,
    lodging_id      TEXT REFERENCES crew_lodging(id) ON DELETE SET NULL,
    flight_id       TEXT REFERENCES flights(id) ON DELETE SET NULL,
    check_in_at     TIMESTAMPTZ NOT NULL,
    check_out_at    TIMESTAMPTZ NOT NULL,
    room_number     TEXT,
    notes           TEXT,
    created_by      TEXT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lodging_assign_crew  ON crew_lodging_assignment(crew_id);
CREATE INDEX IF NOT EXISTS idx_lodging_assign_dates ON crew_lodging_assignment(check_in_at);


-- ── 4. Transport — ground transport bookings ───────────────────────
-- Pickup/drop-off for crew. Direction tells us pickup-from-hotel-to-airport
-- vs the reverse so the dashboard can group both legs.
CREATE TABLE IF NOT EXISTS crew_transport (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    crew_id         TEXT REFERENCES crew(id) ON DELETE CASCADE,
    flight_id       TEXT REFERENCES flights(id) ON DELETE SET NULL,
    direction       TEXT NOT NULL,           -- 'pickup' | 'dropoff'
    pickup_at       TIMESTAMPTZ NOT NULL,
    pickup_location TEXT NOT NULL,
    dropoff_location TEXT NOT NULL,
    vehicle_plate   TEXT,
    driver_name     TEXT,
    driver_phone    TEXT,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'planned',  -- planned | confirmed | done | cancelled
    created_by      TEXT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transport_crew      ON crew_transport(crew_id);
CREATE INDEX IF NOT EXISTS idx_transport_pickup    ON crew_transport(pickup_at);


-- ── 5. IROPS events — operational disruption tracker ──────────────
-- One row per disruptive event. Links to all flights / assignments
-- affected so we can build a "recovery board" for the OCC.
CREATE TABLE IF NOT EXISTS irops_events (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,           -- 'weather' | 'station_closure' | 'strike' | 'security' | 'aog' | 'other'
    title           TEXT NOT NULL,
    description     TEXT,
    affected_station TEXT,                   -- airport code primarily impacted
    severity        TEXT NOT NULL DEFAULT 'major',  -- minor | major | critical
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expected_clear_at TIMESTAMPTZ,
    cleared_at      TIMESTAMPTZ,
    flights_cancelled INT NOT NULL DEFAULT 0,
    crew_affected   INT NOT NULL DEFAULT 0,
    created_by      TEXT REFERENCES users(id),
    closed_by       TEXT REFERENCES users(id),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_irops_active
    ON irops_events(started_at DESC)
    WHERE cleared_at IS NULL;


-- ── RLS — service-role only ────────────────────────────────────────
ALTER TABLE crew_bids                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE crew_lodging              ENABLE ROW LEVEL SECURITY;
ALTER TABLE crew_lodging_assignment   ENABLE ROW LEVEL SECURITY;
ALTER TABLE crew_transport            ENABLE ROW LEVEL SECURITY;
ALTER TABLE irops_events              ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON crew_bids               FROM anon;
REVOKE ALL ON crew_lodging            FROM anon;
REVOKE ALL ON crew_lodging_assignment FROM anon;
REVOKE ALL ON crew_transport          FROM anon;
REVOKE ALL ON irops_events            FROM anon;


-- Touch triggers for updated_at
DROP TRIGGER IF EXISTS crew_bids_touch     ON crew_bids;
CREATE TRIGGER crew_bids_touch     BEFORE UPDATE ON crew_bids
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
DROP TRIGGER IF EXISTS lodging_touch       ON crew_lodging;
CREATE TRIGGER lodging_touch       BEFORE UPDATE ON crew_lodging
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
DROP TRIGGER IF EXISTS transport_touch     ON crew_transport;
CREATE TRIGGER transport_touch     BEFORE UPDATE ON crew_transport
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
DROP TRIGGER IF EXISTS irops_touch         ON irops_events;
CREATE TRIGGER irops_touch         BEFORE UPDATE ON irops_events
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
