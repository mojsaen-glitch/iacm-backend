-- Developer Control Center — adds the `developer` role.
--
-- This role sits ABOVE `super_admin` in the privilege hierarchy:
--   • super_admin  — manages users, flights, observability dashboard
--   • developer    — everything super_admin can do, plus the DCC
--                    (Error Inspector, Safe Actions, Scheduler Debugger, …)
--
-- We do NOT add a CHECK constraint listing every legal role because the
-- existing schema already accepts free-form `role` text; the application
-- layer enforces the gate (see app/api/v1/endpoints/developer_*.py).
--
-- Helper SELECT to confirm the migration succeeded (no rows = role unknown):
--   SELECT id, email, role FROM users WHERE role IN ('developer','super_admin');
--
-- To promote yourself to developer:
--   UPDATE users SET role = 'developer'
--    WHERE email = 'your_email@example.com';
--
-- All existing super_admin permissions stay intact — `developer` is a
-- pure superset, so no data migration is needed.

-- No-op DDL to make this file runnable as a one-shot in Supabase SQL
-- Editor (Supabase rejects scripts that produce nothing). Recording the
-- role rollout in a small bookkeeping table also gives us an audit trail
-- of when DCC was enabled in each environment.

CREATE TABLE IF NOT EXISTS schema_changelog (
    id          SERIAL PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    name        TEXT NOT NULL UNIQUE,
    description TEXT
);

INSERT INTO schema_changelog (name, description)
VALUES ('developer_role_enabled',
        'Added the developer role; promotes a user via UPDATE users SET role=''developer'' WHERE email=...')
ON CONFLICT (name) DO NOTHING;
