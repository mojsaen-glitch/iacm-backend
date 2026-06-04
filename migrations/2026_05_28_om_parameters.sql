-- ════════════════════════════════════════════════════════════════
--  OM operational parameters — Phase A (storage)
--  ───────────────────────────────────────────────────────────────
--  The clause TEXT stays documentation. What the engine will actually apply
--  (Phase B) lives in a structured `parameters` JSON, e.g.:
--    {"reference":"ICAO","rule_kind":"flight_hours_limit",
--     "rolling_window_days":28,"max_hours":100,"warning_threshold_percent":90}
--  Enforcement keys remain bound_check_key + rule_type + category + parameters,
--  never the AR/EN text. Idempotent.
-- ════════════════════════════════════════════════════════════════
ALTER TABLE om_articles
    ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '{}'::jsonb;
