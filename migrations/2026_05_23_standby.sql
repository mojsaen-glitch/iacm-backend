-- ════════════════════════════════════════════════════════════════
--  Standby / Reserve crew management
--
--  A standby record puts a crew member on call for a window. Types:
--    AIRPORT_STANDBY · HOME_STANDBY · READY_RESERVE · LONG_CALL
--  Status lifecycle:
--    ACTIVE → CALLED_OUT → ASSIGNED, plus EXPIRED / CANCELLED
--  Safe to run multiple times.
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS standby_assignments (
    id                 TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    company_id         TEXT NOT NULL,
    crew_id            TEXT NOT NULL REFERENCES crew(id) ON DELETE CASCADE,
    standby_type       TEXT NOT NULL DEFAULT 'AIRPORT_STANDBY',
    airport_code       TEXT,
    start_time         TIMESTAMPTZ NOT NULL,
    end_time           TIMESTAMPTZ NOT NULL,
    response_minutes   INT DEFAULT 60,
    status             TEXT NOT NULL DEFAULT 'ACTIVE',
    called_out         BOOLEAN DEFAULT FALSE,
    assigned_flight_id TEXT REFERENCES flights(id) ON DELETE SET NULL,
    notes              TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_standby_company ON standby_assignments(company_id);
CREATE INDEX IF NOT EXISTS idx_standby_crew    ON standby_assignments(crew_id);
CREATE INDEX IF NOT EXISTS idx_standby_status  ON standby_assignments(status);
CREATE INDEX IF NOT EXISTS idx_standby_window  ON standby_assignments(start_time, end_time);
