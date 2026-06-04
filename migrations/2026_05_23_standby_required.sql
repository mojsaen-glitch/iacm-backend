-- ════════════════════════════════════════════════════════════════
--  Per-flight standby (reserve) requirement
--
--  When a flight is created, the dispatcher specifies how many standby
--  (احتياط) crew the flight needs on call. This is a planning target;
--  the actual reserves are managed in standby_assignments and ranked
--  per flight via GET /standby/suggest/{flight_id}.
--
--  Safe to run multiple times.
-- ════════════════════════════════════════════════════════════════

ALTER TABLE flights
    ADD COLUMN IF NOT EXISTS standby_required INT DEFAULT 0;
