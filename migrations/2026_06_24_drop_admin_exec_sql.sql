-- Reverses migrations/2026_06_23_admin_exec_sql.sql — the SQL Editor
-- feature was removed at the user's request, so the SECURITY DEFINER
-- function it called is no longer needed. Direct DB access stays via
-- Supabase Studio's built-in SQL editor.
--
-- Idempotent: IF EXISTS so re-runs are no-ops.

DROP FUNCTION IF EXISTS admin_exec_sql(text);
