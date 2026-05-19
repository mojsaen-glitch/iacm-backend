-- ─────────────────────────────────────────────────────────────────
-- Aircraft operational status — Sprint 3 fix
--
-- Sprint 1 added a local SQLite column for AOG state, but the Supabase
-- aircraft table never got the same columns. Now that defects (Sprint 3)
-- auto-flip the Supabase row to 'aog', we need the columns to exist.
-- ─────────────────────────────────────────────────────────────────

ALTER TABLE aircraft
    ADD COLUMN IF NOT EXISTS operational_status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS status_reason      TEXT,
    ADD COLUMN IF NOT EXISTS status_changed_at  TIMESTAMPTZ;

-- Drop the constraint first in case an earlier run already added it
ALTER TABLE aircraft DROP CONSTRAINT IF EXISTS aircraft_operational_status_chk;
ALTER TABLE aircraft
    ADD CONSTRAINT aircraft_operational_status_chk
    CHECK (operational_status IN ('active','maintenance','aog','grounded'));

-- Backfill: anything that was is_active=true gets 'active', everything
-- else 'grounded' (long-term withdrawn). Admin can correct individual
-- rows from the fleet page.
UPDATE aircraft
   SET operational_status = CASE WHEN is_active THEN 'active' ELSE 'grounded' END
 WHERE operational_status IS NULL OR operational_status = '';

CREATE INDEX IF NOT EXISTS idx_aircraft_op_status ON aircraft(operational_status);
