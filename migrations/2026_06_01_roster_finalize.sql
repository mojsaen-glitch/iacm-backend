-- ─────────────────────────────────────────────────────────────────────
-- Roster finalisation state — makes POST /flights/{id}/finalize-roster
-- idempotent and reportable.
--
-- Run ONCE in the Supabase SQL Editor. Safe to re-run (IF NOT EXISTS).
-- Until this runs, finalize-roster still works (gate + notify) but cannot
-- remember that a flight was already finalised, so it is not idempotent yet.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE flights
    ADD COLUMN IF NOT EXISTS roster_finalized_status TEXT,            -- 'finalized' | NULL
    ADD COLUMN IF NOT EXISTS roster_finalized_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS roster_finalized_by      TEXT REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_flights_roster_finalized
    ON flights (roster_finalized_status);
