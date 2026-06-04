-- ════════════════════════════════════════════════════════════════
--  OM as the rules engine — Phase 1 (schema + bindings + audit)
--  ───────────────────────────────────────────────────────────────
--  Turns Operations-Manual clauses (om_articles) into the control plane over
--  the compliance engine's hardcoded checks. An article BINDS to a check
--  family via `bound_check_key` and governs it: whether it's active, whether
--  it BLOCKS / WARNS / needs approval / is informational, and which clause
--  number is stamped on every resulting violation message.
--
--  Safe to run more than once (idempotent: ADD COLUMN IF NOT EXISTS, CREATE
--  TABLE IF NOT EXISTS, UPDATE by id). Never overwrites article text.
-- ════════════════════════════════════════════════════════════════

-- ── 1. New governance columns on om_articles ──────────────────────
ALTER TABLE om_articles
    ADD COLUMN IF NOT EXISTS rule_type          TEXT    NOT NULL DEFAULT 'informational',
    ADD COLUMN IF NOT EXISTS category           TEXT,
    ADD COLUMN IF NOT EXISTS affects_compliance BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS bound_check_key    TEXT;

-- rule_type domain: informational | warning | blocking | approval_required
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'om_articles_rule_type_chk'
  ) THEN
    ALTER TABLE om_articles
      ADD CONSTRAINT om_articles_rule_type_chk
      CHECK (rule_type IN ('informational','warning','blocking','approval_required'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_om_articles_bound_check ON om_articles(bound_check_key);
CREATE INDEX IF NOT EXISTS idx_om_articles_affects     ON om_articles(affects_compliance);

-- ── 2. Audit log — every governance change to a clause ────────────
CREATE TABLE IF NOT EXISTS om_rule_audit_logs (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    article_id  TEXT NOT NULL,
    company_id  TEXT,
    action      TEXT NOT NULL,          -- create | update | delete | toggle
    changed_by  TEXT,                   -- user id
    before      JSONB,
    after       JSONB,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_om_audit_article ON om_rule_audit_logs(article_id);
CREATE INDEX IF NOT EXISTS idx_om_audit_created ON om_rule_audit_logs(created_at);

ALTER TABLE om_rule_audit_logs ENABLE ROW LEVEL SECURITY;  -- deny-by-default; access via API only

-- ── 3. Bind the already-seeded clauses to their engine checks ─────
-- UPDATE-only: affects existing rows, never inserts, never touches text.
-- Families with no default clause yet (documents, crew_status, fdp,
-- flight_hours_monthly, aircraft_qualification, assignment_conflict) are bound
-- later from the OM control-center page.
UPDATE om_articles SET affects_compliance = TRUE, rule_type = 'blocking',
       category = 'fatigue', bound_check_key = 'flight_hours_28day'
 WHERE id = 'OM-C 8.1';

UPDATE om_articles SET affects_compliance = TRUE, rule_type = 'blocking',
       category = 'fatigue', bound_check_key = 'flight_hours_yearly'
 WHERE id = 'OM-C 8.2';

UPDATE om_articles SET affects_compliance = TRUE, rule_type = 'blocking',
       category = 'fatigue', bound_check_key = 'rest'
 WHERE id = 'OM-C 9.1';

UPDATE om_articles SET affects_compliance = TRUE, rule_type = 'blocking',
       category = 'training', bound_check_key = 'training'
 WHERE id = 'OM-D 2.1';
