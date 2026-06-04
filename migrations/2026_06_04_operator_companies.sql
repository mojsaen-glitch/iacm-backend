-- Multi-airline (operator company) support.
--
-- Design (per product decision): the EXISTING `companies` table is reused as the
-- AIRLINE / operator registry. The tenant `company_id` columns stay UNCHANGED (so
-- RBAC / Dashboard / Monthly-Matrix scoping is not affected). A new, nullable
-- `operator_company_id` is the airline a crew member belongs to — snapshotted onto
-- assignments at assignment time, and an optional operating airline on flights.
-- A flight may carry crew from MULTIPLE operator companies (no restriction added).
--
-- Idempotent. Run on staging first.

ALTER TABLE crew        ADD COLUMN IF NOT EXISTS operator_company_id TEXT REFERENCES companies(id);
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS operator_company_id TEXT REFERENCES companies(id);
ALTER TABLE flights     ADD COLUMN IF NOT EXISTS operator_company_id TEXT REFERENCES companies(id);

CREATE INDEX IF NOT EXISTS idx_crew_operator_company        ON crew (operator_company_id);
CREATE INDEX IF NOT EXISTS idx_assignments_operator_company ON assignments (operator_company_id);

-- Backfill: existing crew default to their own tenant company as their airline, so
-- nobody is left without an operator (every crew "must belong to a company").
UPDATE crew SET operator_company_id = company_id WHERE operator_company_id IS NULL;
