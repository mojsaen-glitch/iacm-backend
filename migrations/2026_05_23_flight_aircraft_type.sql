-- ════════════════════════════════════════════════════════════════
--  Per-flight aircraft type (denormalized for type-rating checks)
--
--  The add-flight form already collects an aircraft type (B737/B788/A320…)
--  but never stored it. Persisting it on the flight lets the compliance
--  engine block crew who are not type-rated for that aircraft.
--
--  Safe to run multiple times.
-- ════════════════════════════════════════════════════════════════

ALTER TABLE flights
    ADD COLUMN IF NOT EXISTS aircraft_type TEXT;
