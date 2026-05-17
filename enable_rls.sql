-- ============================================================================
-- IACM — Row Level Security (RLS) Setup
-- ============================================================================
-- HOW TO RUN:
--   1. افتح https://supabase.com/dashboard
--   2. اختر مشروع hfqwzibamphaphdkjpue
--   3. اضغط SQL Editor (أيقونة 📝 في القائمة اليسرى)
--   4. اضغط "+ New query"
--   5. الصق هذا الملف كاملاً
--   6. اضغط Run (Ctrl+Enter)
--
-- NOTE: الـ backend يستخدم service_role key الذي يتجاوز RLS تلقائياً، فلن يتأثر.
-- هذه طبقة دفاع إضافية لو تسرّب المفتاح يوماً.
-- ============================================================================

-- ── خطوة 1: تفعيل RLS على الجداول الرئيسية ────────────────────────────────
ALTER TABLE crew              ENABLE ROW LEVEL SECURITY;
ALTER TABLE flights           ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignments       ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents         ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_records  ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications     ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages          ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_requests    ENABLE ROW LEVEL SECURITY;
ALTER TABLE aircraft          ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings          ENABLE ROW LEVEL SECURITY;

-- ── خطوة 2: إسقاط أي policies قديمة بنفس الاسم (idempotent) ──────────────
DROP POLICY IF EXISTS "company_isolation_crew"          ON crew;
DROP POLICY IF EXISTS "company_isolation_flights"       ON flights;
DROP POLICY IF EXISTS "company_isolation_assignments"   ON assignments;
DROP POLICY IF EXISTS "company_isolation_documents"     ON documents;
DROP POLICY IF EXISTS "company_isolation_training"      ON training_records;
DROP POLICY IF EXISTS "company_isolation_notifications" ON notifications;
DROP POLICY IF EXISTS "company_isolation_messages"      ON messages;
DROP POLICY IF EXISTS "company_isolation_audit"         ON audit_log;
DROP POLICY IF EXISTS "company_isolation_leave"         ON leave_requests;
DROP POLICY IF EXISTS "company_isolation_aircraft"      ON aircraft;
DROP POLICY IF EXISTS "company_isolation_settings"      ON settings;

-- ── خطوة 3: إنشاء policies جديدة (عزل بالشركة) ─────────────────────────
-- الجداول التي تملك company_id مباشرة
CREATE POLICY "company_isolation_crew" ON crew FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY "company_isolation_flights" ON flights FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY "company_isolation_notifications" ON notifications FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY "company_isolation_messages" ON messages FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY "company_isolation_audit" ON audit_log FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY "company_isolation_aircraft" ON aircraft FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY "company_isolation_settings" ON settings FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- الجداول التي ترث company_id من crew
CREATE POLICY "company_isolation_documents" ON documents FOR ALL TO authenticated
  USING (crew_id IN (
    SELECT id FROM crew WHERE company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

CREATE POLICY "company_isolation_training" ON training_records FOR ALL TO authenticated
  USING (crew_id IN (
    SELECT id FROM crew WHERE company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

CREATE POLICY "company_isolation_leave" ON leave_requests FOR ALL TO authenticated
  USING (crew_id IN (
    SELECT id FROM crew WHERE company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

-- الجدول الذي يرث company_id من flights
CREATE POLICY "company_isolation_assignments" ON assignments FOR ALL TO authenticated
  USING (flight_id IN (
    SELECT id FROM flights WHERE company_id = (auth.jwt() ->> 'company_id')::uuid
  ));

-- ── خطوة 4: التحقق ────────────────────────────────────────────────────────
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('crew','flights','assignments','documents','training_records',
                    'notifications','messages','audit_log','leave_requests',
                    'aircraft','settings')
ORDER BY tablename;
-- يجب أن تشاهد rowsecurity = true لكل الجداول

SELECT schemaname, tablename, policyname
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;
-- يجب أن تشاهد 11 policy واحدة لكل جدول
