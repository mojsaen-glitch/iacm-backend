-- Load-capacity indexes (target: 3000 crew · ~3000 flights/month · ~18000 assignments/month).
-- Derived from the Backend Architecture & Load Capacity Audit (tasks/backend_audit.md).
--
-- All indexes are ADDITIVE and IF NOT EXISTS (safe to re-run). On a LARGE live table,
-- create them CONCURRENTLY to avoid write locks (run each statement separately, NOT in a
-- transaction):  CREATE INDEX CONCURRENTLY ...   The plain form below is fine on the
-- current small dataset and on staging.
--
-- ⚠ Run on STAGING first and verify with EXPLAIN ANALYZE before applying to production.

-- Dashboard + flight list: company-scoped time-range scans (today / week / month).
-- Existing indexes are flights(company_id) and flights(departure_time) SEPARATELY;
-- the composite lets a single index satisfy "this company, this time window".
CREATE INDEX IF NOT EXISTS idx_flights_company_departure
    ON flights (company_id, departure_time);

-- Crew roster filtering by rank / base within a company (3000-crew lists + matrix filters).
CREATE INDEX IF NOT EXISTS idx_crew_company_rank
    ON crew (company_id, rank);
CREATE INDEX IF NOT EXISTS idx_crew_company_base
    ON crew (company_id, base);

-- Compliance / document-expiry alerts on the dashboard (per-crew expiry windows).
CREATE INDEX IF NOT EXISTS idx_documents_crew_expiry
    ON documents (crew_id, expiry_date);

-- Notification feed: newest-first per recipient (column is target_user_id).
CREATE INDEX IF NOT EXISTS idx_notifications_user_created
    ON notifications (target_user_id, created_at DESC);

-- Crew flight-history pagination (statement + crew/{id}/flights): scope by crew, newest first.
CREATE INDEX IF NOT EXISTS idx_assignments_crew_created
    ON assignments (crew_id, created_at DESC);

-- Roster + matrix pivot on (flight_id, duty_type). A full composite complements the
-- existing partial "operating only" index for mixed duty-type reads.
CREATE INDEX IF NOT EXISTS idx_assignments_flight_duty
    ON assignments (flight_id, duty_type);

-- NOTE (NOT auto-applied — schema change, see report Tier-4):
--   The biggest structural win is denormalising company_id onto `assignments`
--   (today every company-scoped assignment query must join through flights). After a
--   backfill + populating it on insert, add:
--     ALTER TABLE assignments ADD COLUMN company_id TEXT;
--     CREATE INDEX idx_assignments_company_crew  ON assignments (company_id, crew_id);
--     CREATE INDEX idx_assignments_company_flight ON assignments (company_id, flight_id);
