-- ─────────────────────────────────────────────────────────────────
-- Assignment decline workflow
--
-- Adds columns so a crew member can refuse an assignment with a reason.
-- The row stays in the table (audit trail) — scheduler creates a new
-- assignment for the replacement crew.
-- ─────────────────────────────────────────────────────────────────

ALTER TABLE assignments
    ADD COLUMN IF NOT EXISTS declined        BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS decline_reason  TEXT,
    ADD COLUMN IF NOT EXISTS declined_at     TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_assignments_declined
    ON assignments(declined)
    WHERE declined IS TRUE;
