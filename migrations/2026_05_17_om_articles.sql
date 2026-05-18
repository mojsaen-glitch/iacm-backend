-- ─────────────────────────────────────────────────────────────────
-- Operations Manual articles
--
-- Stores editable OM clauses. The Dart static catalog still ships as
-- a fallback for offline / first-run, but anything the admin saves
-- through the Settings page lives here and overrides it.
--
-- RLS: deny-by-default. Read = any authenticated user from the same
-- company (filtered by visible_to_roles in the endpoint). Write =
-- admin / ops_manager only — service role for the FastAPI worker.
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS om_articles (
    id                 TEXT PRIMARY KEY,                -- e.g. "OM-A 8.1"
    company_id         TEXT REFERENCES companies(id) ON DELETE CASCADE,
    section            TEXT NOT NULL CHECK (section IN ('A','B','C','D')),
    chapter_ar         TEXT NOT NULL DEFAULT '',
    chapter_en         TEXT NOT NULL DEFAULT '',
    title_ar           TEXT NOT NULL,
    title_en           TEXT NOT NULL,
    body_ar            TEXT NOT NULL,
    body_en            TEXT NOT NULL,
    visible_to_roles   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[], -- empty = all roles
    linked_rule_id     TEXT,
    linked_route       TEXT,
    sort_order         INT NOT NULL DEFAULT 0,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_by         TEXT REFERENCES users(id),
    updated_by         TEXT REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_om_articles_company  ON om_articles(company_id);
CREATE INDEX IF NOT EXISTS idx_om_articles_section  ON om_articles(section);
CREATE INDEX IF NOT EXISTS idx_om_articles_active   ON om_articles(is_active);

-- Deny-by-default RLS. Reads pass through the FastAPI endpoint which
-- applies role-based visibility, so we don't need fine-grained policies
-- here — just block direct anon access.
ALTER TABLE om_articles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON om_articles FROM anon;

-- Keep updated_at fresh on PATCH
CREATE OR REPLACE FUNCTION om_articles_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS om_articles_touch ON om_articles;
CREATE TRIGGER om_articles_touch
    BEFORE UPDATE ON om_articles
    FOR EACH ROW EXECUTE FUNCTION om_articles_touch_updated_at();
