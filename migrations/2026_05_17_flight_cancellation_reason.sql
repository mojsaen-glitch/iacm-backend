-- ─────────────────────────────────────────────────────────────────
-- Cancellation / delay / diversion reason capture on flights
--
-- Lets the cancel/delay dialog record WHY a flight didn't operate so
-- we can answer "weather vs technical vs crew" in the OTP report.
-- Backwards compatible: existing rows stay NULL.
-- ─────────────────────────────────────────────────────────────────

ALTER TABLE flights
    ADD COLUMN IF NOT EXISTS cancellation_reason TEXT,
    ADD COLUMN IF NOT EXISTS cancellation_notes  TEXT,
    ADD COLUMN IF NOT EXISTS delay_minutes       INT;

-- Index so dashboards can group by reason quickly.
CREATE INDEX IF NOT EXISTS idx_flights_cancel_reason
    ON flights(cancellation_reason)
    WHERE cancellation_reason IS NOT NULL;
