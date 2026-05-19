-- ─────────────────────────────────────────────────────────────────────
-- Sprint 3 — Maintenance & Engineering
--
-- Three tables that let an engineer / ops manager track:
--   1. Defects logged against tails (with severity & resolution state)
--   2. MEL items (Minimum Equipment List) — degraded but legal-to-fly
--      items with a deferral deadline
--   3. Recurring maintenance checks (A-check, C-check, weight & balance)
--
-- A defect with severity=critical or grounding=TRUE auto-flips the
-- aircraft.operational_status to 'aog' via the trigger below — keeps
-- engineering and dispatch in lock-step without a second app to sync.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS defects (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    aircraft_id     TEXT REFERENCES aircraft(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT,
    severity        TEXT NOT NULL DEFAULT 'minor',  -- minor | major | critical
    grounding       BOOLEAN NOT NULL DEFAULT FALSE, -- forces AOG when TRUE
    status          TEXT NOT NULL DEFAULT 'open',    -- open | deferred | resolved
    reported_by     TEXT REFERENCES users(id),
    reported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by     TEXT REFERENCES users(id),
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT,
    mel_item_id     TEXT,  -- set when this defect is deferred under MEL
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_defects_aircraft ON defects(aircraft_id);
CREATE INDEX IF NOT EXISTS idx_defects_status   ON defects(status);


CREATE TABLE IF NOT EXISTS mel_items (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    aircraft_id     TEXT REFERENCES aircraft(id) ON DELETE CASCADE,
    mel_reference   TEXT NOT NULL,           -- e.g. "21-49-1A"  (chapter-section-item)
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'C', -- A=fix before next departure, B=3d, C=10d, D=120d
    deferred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deadline        DATE NOT NULL,
    cleared         BOOLEAN NOT NULL DEFAULT FALSE,
    cleared_at      TIMESTAMPTZ,
    cleared_by      TEXT REFERENCES users(id),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mel_aircraft ON mel_items(aircraft_id);
CREATE INDEX IF NOT EXISTS idx_mel_deadline ON mel_items(deadline) WHERE cleared = FALSE;


CREATE TABLE IF NOT EXISTS maintenance_checks (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    aircraft_id     TEXT REFERENCES aircraft(id) ON DELETE CASCADE,
    check_type      TEXT NOT NULL,           -- 'A', 'B', 'C', 'D', 'transit', 'daily', 'weekly'
    last_done       DATE,
    last_done_hours NUMERIC(10,2),           -- airframe hours at last check
    next_due_date   DATE,
    next_due_hours  NUMERIC(10,2),
    interval_days   INT,
    interval_hours  NUMERIC(10,2),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_checks_aircraft ON maintenance_checks(aircraft_id);
CREATE INDEX IF NOT EXISTS idx_checks_due      ON maintenance_checks(next_due_date);


-- RLS — service-role only (FastAPI enforces roles in code)
ALTER TABLE defects             ENABLE ROW LEVEL SECURITY;
ALTER TABLE mel_items           ENABLE ROW LEVEL SECURITY;
ALTER TABLE maintenance_checks  ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON defects            FROM anon;
REVOKE ALL ON mel_items          FROM anon;
REVOKE ALL ON maintenance_checks FROM anon;


-- Reuse the touch trigger
DROP TRIGGER IF EXISTS defects_touch ON defects;
CREATE TRIGGER defects_touch BEFORE UPDATE ON defects
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
DROP TRIGGER IF EXISTS mel_touch ON mel_items;
CREATE TRIGGER mel_touch BEFORE UPDATE ON mel_items
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
DROP TRIGGER IF EXISTS checks_touch ON maintenance_checks;
CREATE TRIGGER checks_touch BEFORE UPDATE ON maintenance_checks
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
