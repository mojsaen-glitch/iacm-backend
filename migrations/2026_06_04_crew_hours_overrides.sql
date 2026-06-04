-- Crew Monthly Flight Hours — manual overrides + audit log (Phase 2).
--
-- A super-admin may override the credited hours of a specific crew member on a
-- specific day. The override REPLACES the computed day hours in the matrix and
-- the Excel export. Every change is appended to an immutable audit log with the
-- old/new value, reason (mandatory), note, who, role and time.
--
-- Idempotent: safe to re-run.

-- Current override per (company, crew, day). Upserted; one active row per day.
CREATE TABLE IF NOT EXISTS crew_hours_overrides (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    company_id      TEXT NOT NULL,
    crew_id         TEXT NOT NULL,
    duty_date       DATE NOT NULL,
    override_hours  FLOAT NOT NULL,
    old_value       FLOAT,
    reason          TEXT NOT NULL,
    note            TEXT,
    created_by      TEXT,
    created_by_name TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (company_id, crew_id, duty_date)
);

CREATE INDEX IF NOT EXISTS idx_crew_hours_overrides_lookup
    ON crew_hours_overrides (company_id, crew_id, duty_date);

-- Append-only audit trail of every manual change (set or clear).
CREATE TABLE IF NOT EXISTS crew_hours_audit_log (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    company_id        TEXT NOT NULL,
    crew_id           TEXT NOT NULL,
    duty_date         DATE NOT NULL,
    action            TEXT NOT NULL,          -- 'set' | 'clear'
    old_value         FLOAT,
    new_value         FLOAT,
    reason            TEXT,
    note              TEXT,
    performed_by      TEXT,
    performed_by_name TEXT,
    performed_role    TEXT,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crew_hours_audit_crew
    ON crew_hours_audit_log (company_id, crew_id, created_at DESC);
