-- ─────────────────────────────────────────────────────────────────────
-- Remove the temporary test-account seeding feature "from the roots".
--
-- Run this ONCE in the Supabase SQL Editor. It:
--   1. Deletes every seeded TEST CREW row (markers), then their assignments.
--   2. Deletes every seeded TEST USER row (markers).
--   3. Drops the marker columns from both tables.
--
-- Order matters: rows are removed by their markers BEFORE the marker columns
-- are dropped (afterwards you could no longer identify them). Real accounts
-- are never touched — only rows explicitly flagged is_test_*/created_by_seed.
-- ─────────────────────────────────────────────────────────────────────

BEGIN;

-- 1) Assignments belonging to seeded test crew (FK cleanup first).
DELETE FROM assignments
 WHERE crew_id IN (
     SELECT id FROM crew
      WHERE is_test_crew = true OR created_by_seed = true
 );

-- 2) Seeded test crew rows.
DELETE FROM crew
 WHERE is_test_crew = true OR created_by_seed = true;

-- 3) Seeded test user rows.
DELETE FROM users
 WHERE is_test_account = true OR created_by_seed = true;

-- 4) Drop the marker columns + their indexes (IF EXISTS = safe to re-run).
DROP INDEX IF EXISTS idx_crew_seed_batch;
DROP INDEX IF EXISTS idx_crew_is_test;
DROP INDEX IF EXISTS idx_users_seed_batch;
DROP INDEX IF EXISTS idx_users_is_test;

ALTER TABLE crew
    DROP COLUMN IF EXISTS is_test_crew,
    DROP COLUMN IF EXISTS created_by_seed,
    DROP COLUMN IF EXISTS seed_batch_id,
    DROP COLUMN IF EXISTS seed_label,
    DROP COLUMN IF EXISTS linked_user_id;

ALTER TABLE users
    DROP COLUMN IF EXISTS is_test_account,
    DROP COLUMN IF EXISTS created_by_seed,
    DROP COLUMN IF EXISTS seed_batch_id,
    DROP COLUMN IF EXISTS seed_label;

COMMIT;
