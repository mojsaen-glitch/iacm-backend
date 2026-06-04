-- ─────────────────────────────────────────────────────────────────────
-- Assignment category + role: cleanly separate REAL aircraft crew from
-- operational ground staff on a flight.
--
--   assignment_type ∈ { 'flight_deck' | 'cabin_crew' | 'ground_operations' }
--   assigned_role   = the crew.rank captured at assignment time (audit / display)
--
-- The CATEGORY is always derivable from crew.rank (single source of truth) —
-- this column just stores the resolved bucket so flight rosters render in 3
-- sections without re-joining, and so legacy rows (assignment_type='regular'
-- or NULL) are normalised. The backend re-derives on every new assignment, so
-- a wrong/missing value here never breaks logic — it self-heals.
--
-- Run ONCE in the Supabase SQL Editor. Safe to re-run (idempotent).
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE assignments
    ADD COLUMN IF NOT EXISTS assigned_role TEXT;

-- Backfill ONLY legacy rows (NULL or the old 'regular' default). Rows that
-- already carry a real category value are left untouched.
UPDATE assignments a
SET
    assigned_role = c.rank,
    assignment_type = CASE lower(coalesce(c.rank, ''))
        WHEN 'captain'         THEN 'flight_deck'
        WHEN 'first_officer'   THEN 'flight_deck'
        WHEN 'second_officer'  THEN 'flight_deck'
        WHEN 'flight_engineer' THEN 'flight_deck'
        WHEN 'chief'           THEN 'cabin_crew'
        WHEN 'purser'          THEN 'cabin_crew'
        WHEN 'senior'          THEN 'cabin_crew'
        WHEN 'cabin_crew'      THEN 'cabin_crew'
        WHEN 'dispatcher'      THEN 'ground_operations'
        WHEN 'ground_staff'    THEN 'ground_operations'
        -- legacy short codes, just in case old data used them
        WHEN 'pic'  THEN 'flight_deck'
        WHEN 'cpt'  THEN 'flight_deck'
        WHEN 'sic'  THEN 'flight_deck'
        WHEN 'fo'   THEN 'flight_deck'
        WHEN 'so'   THEN 'flight_deck'
        WHEN 'fe'   THEN 'flight_deck'
        WHEN 'chf'  THEN 'cabin_crew'
        WHEN 'pur'  THEN 'cabin_crew'
        WHEN 'scc'  THEN 'cabin_crew'
        WHEN 'cc'   THEN 'cabin_crew'
        WHEN 'dsp'  THEN 'ground_operations'
        WHEN 'gnd'  THEN 'ground_operations'
        -- unknown rank → operational bucket (mirrors category_for_rank's
        -- 'other', so it is NEVER counted in aircraft complement by mistake)
        ELSE 'ground_operations'
    END
FROM crew c
WHERE a.crew_id = c.id
  AND (a.assignment_type IS NULL OR a.assignment_type = 'regular');

CREATE INDEX IF NOT EXISTS idx_assignments_type ON assignments (assignment_type);
