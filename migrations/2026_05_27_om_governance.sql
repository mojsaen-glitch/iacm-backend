-- ════════════════════════════════════════════════════════════════
--  OM safety-governance gate — audit columns
--  Adds the governance_reason + is_safety_critical fields the gate records
--  whenever a Safety-Critical OM clause is weakened (disable / downgrade /
--  rebind / unbind), which is restricted to Super Admin and requires a reason.
--  Idempotent.
-- ════════════════════════════════════════════════════════════════
ALTER TABLE om_rule_audit_logs
    ADD COLUMN IF NOT EXISTS governance_reason  TEXT,
    ADD COLUMN IF NOT EXISTS is_safety_critical BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_om_audit_safety
    ON om_rule_audit_logs(is_safety_critical);
