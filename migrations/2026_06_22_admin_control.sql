-- M5 — Full-control admin tables.
--
-- `feature_flags`  — hot-toggleable on/off switches the API checks at runtime
--                    (e.g. "ai_scheduling" off during maintenance windows).
-- `system_config`  — singleton-style key/value store for global toggles
--                    like maintenance_mode + the message shown to users.
-- Both are super-admin-only via the application layer.

CREATE TABLE IF NOT EXISTS feature_flags (
    key         TEXT PRIMARY KEY,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID
);

CREATE TABLE IF NOT EXISTS system_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID
);

-- Seed the two singletons the dashboard expects.
INSERT INTO system_config (key, value)
VALUES ('maintenance_mode', '{"enabled": false, "message": "النظام تحت الصيانة المؤقتة", "allow_super_admin": true}'::jsonb)
ON CONFLICT (key) DO NOTHING;

INSERT INTO feature_flags (key, enabled, description) VALUES
  ('ai_scheduling',    true,  'تشغيل/إيقاف الجدولة الذكية'),
  ('push_notifications', true, 'إرسال إشعارات Push للموبايل'),
  ('auto_assign',      true,  'الـ auto-assign optimizer')
ON CONFLICT (key) DO NOTHING;
