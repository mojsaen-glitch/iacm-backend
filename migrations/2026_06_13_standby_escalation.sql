-- ════════════════════════════════════════════════════════════════
--  Standby / Reserve — R3: auto-escalation on no-response / rejection
--
--  When a called-out reserve does not respond within its response window,
--  or rejects, the escalation sweep moves the callout to the next valid
--  candidate (same `suggest` ranking) — or alerts ops if none remain.
--
--    called_out_at     : when the reserve was called out (anchors the
--                        no-response timeout = called_out_at + response_minutes)
--    escalated_at      : set once a failed callout has been processed
--                        (idempotency guard — a re-run never re-escalates)
--    escalation_status : ESCALATED | EXHAUSTED
--
--  Purely additive. Safe to run multiple times. No data is modified.
-- ════════════════════════════════════════════════════════════════

ALTER TABLE standby_assignments
    ADD COLUMN IF NOT EXISTS called_out_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS escalated_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS escalation_status TEXT;
