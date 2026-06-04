-- ════════════════════════════════════════════════════════════════
--  Crew roster short-name (unique per company)
--  Adds a short "roster name" identifier each crew member can be
--  referred to by on rosters. It must be UNIQUE across the company —
--  no two crew (cabin or cockpit) may share the same roster name.
--
--  Uniqueness is case-insensitive and ignores NULLs, so existing crew
--  without a roster name are unaffected until one is set.
--  Safe to run multiple times.
-- ════════════════════════════════════════════════════════════════

ALTER TABLE crew ADD COLUMN IF NOT EXISTS roster_name TEXT;

-- Case-insensitive uniqueness, scoped per company, NULLs allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_crew_roster_name
    ON crew (company_id, lower(roster_name))
    WHERE roster_name IS NOT NULL;
