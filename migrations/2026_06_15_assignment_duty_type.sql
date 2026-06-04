-- Deadhead / Positioning Crew — distinguishes a crew member who is RIDING the
-- flight (deadhead / standby / observer / training) from one who is OPERATING
-- it. Only `operating` rows count toward the GenDec complement, the per-role
-- over-staffing cap, and the minimum-crew gate on roster finalisation.
--
-- The column is added with a default of 'operating' so EVERY existing row
-- keeps the same semantics it had before this migration (zero data drift) —
-- there's no need to backfill.
--
-- Idempotent: re-runs are no-ops thanks to IF NOT EXISTS.

ALTER TABLE assignments
    ADD COLUMN IF NOT EXISTS duty_type TEXT NOT NULL DEFAULT 'operating';

-- Enumerate the legal values at the database boundary so a typo never sneaks
-- past application code (e.g. via a SQL editor or future endpoint).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage
        WHERE  table_name = 'assignments'
          AND  constraint_name = 'assignments_duty_type_check'
    ) THEN
        ALTER TABLE assignments
            ADD CONSTRAINT assignments_duty_type_check
            CHECK (duty_type IN
                ('operating', 'deadhead', 'standby', 'observer', 'training'));
    END IF;
END $$;

-- Index for the common predicate the roster + capacity gates use:
--   WHERE flight_id = $1 AND duty_type = 'operating'
-- A partial index keeps the index small (non-operating rows are rare).
CREATE INDEX IF NOT EXISTS assignments_flight_operating_idx
    ON assignments (flight_id)
    WHERE duty_type = 'operating';
