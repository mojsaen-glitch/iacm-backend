-- Actual movement times (ATD/ATA) — Phase 1 of the actual-hours model.
-- STD/STA (departure_time/arrival_time) stay the SCHEDULE; ETD/ETA stay the
-- delay layer (estimated_departure_time/delay_*); ATD/ATA are an INDEPENDENT
-- layer recorded explicitly from OCC (never auto-written by status changes).
-- Editing a recorded value requires a mandatory reason (full before/after audit).
alter table flights
  add column if not exists actual_departure_time timestamptz,   -- ATD
  add column if not exists actual_arrival_time   timestamptz,   -- ATA
  add column if not exists actual_times_updated_by text,
  add column if not exists actual_times_updated_at timestamptz;
