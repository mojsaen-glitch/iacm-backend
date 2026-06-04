-- ─────────────────────────────────────────────────────────────────────
-- Messaging: allow crew (by crew_id) as message participants, so a crew
-- member can message ALL crew on their flight even when those crew have no
-- `users` login account yet. A participant is now either a USER (user_id) or
-- a CREW member (crew_id).
--
-- Run ONCE in the Supabase SQL Editor. Safe to re-run.
--   • receiver_id becomes NULLable (a crew receiver has receiver_crew_id set
--     and receiver_id NULL).
--   • sender stays a logged-in user (sender_id NOT NULL) but also records
--     sender_crew_id when the sender is crew.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE messages
    ALTER COLUMN receiver_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS sender_crew_id   TEXT,
    ADD COLUMN IF NOT EXISTS receiver_crew_id TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_receiver_crew ON messages (receiver_crew_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_crew   ON messages (sender_crew_id);
