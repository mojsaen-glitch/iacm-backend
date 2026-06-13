-- ════════════════════════════════════════════════════════════════
--  Standby / Reserve — R2: crew response to a callout + assignment link
--
--  A called-out reserve can now ACCEPT or REJECT (with a reason). On
--  accept, the assignment is created through the EXISTING /assignments
--  path (all safety gates + the existing assignment audit apply); the
--  created assignment's id is linked back here. If that assignment fails
--  the gate, `assignment_error` records why — acceptance is NOT a
--  successful tasking until `assignment_id` is set.
--
--  Purely additive. Safe to run multiple times. No data is modified.
-- ════════════════════════════════════════════════════════════════

ALTER TABLE standby_assignments
    ADD COLUMN IF NOT EXISTS response_status   TEXT,          -- ACCEPTED | REJECTED | NULL
    ADD COLUMN IF NOT EXISTS response_reason   TEXT,          -- reject reason (required on reject)
    ADD COLUMN IF NOT EXISTS responded_at      TIMESTAMPTZ,   -- when the crew responded
    ADD COLUMN IF NOT EXISTS assignment_id     TEXT,          -- created assignment (accept → assigned)
    ADD COLUMN IF NOT EXISTS assignment_error  TEXT;          -- accept-but-assign-failed reason (visible)

CREATE INDEX IF NOT EXISTS idx_standby_response
    ON standby_assignments(response_status)
    WHERE response_status IS NOT NULL;
