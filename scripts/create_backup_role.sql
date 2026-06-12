-- IACM — read-only backup role for pg_dump (phase 0).
-- Run in Supabase SQL Editor. REPLACE the placeholder password BEFORE running
-- and NEVER commit the real one anywhere. pg_dump needs SELECT only.

create role iacm_backup
  login
  password 'REPLACE_WITH_A_STRONG_GENERATED_PASSWORD'   -- ← غيّرها قبل التنفيذ
  noinherit;

grant connect on database postgres to iacm_backup;
grant usage  on schema public  to iacm_backup;

-- Existing objects:
grant select on all tables    in schema public to iacm_backup;
grant select on all sequences in schema public to iacm_backup;

-- Future objects (new feature tables keep being dumpable automatically):
alter default privileges in schema public grant select on tables    to iacm_backup;
alter default privileges in schema public grant select on sequences to iacm_backup;

-- ─────────────────────────────────────────────────────────────────────────────
-- Connection string for DATABASE_BACKUP_URL (fill host/ref from your
-- dashboard — Settings → Database → Connection string):
--
--   postgresql://iacm_backup.<PROJECT_REF>:<PASSWORD>@<SESSION_POOLER_HOST>:5432/postgres
--
-- IMPORTANT: use the SESSION pooler (port 5432) or the direct connection —
-- pg_dump does NOT work through the transaction pooler (port 6543).
-- ─────────────────────────────────────────────────────────────────────────────

-- Rollback (if ever needed):
--   drop owned by iacm_backup; drop role iacm_backup;
