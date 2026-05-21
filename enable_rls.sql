-- ============================================================================
-- IACM — Row Level Security (RLS) Setup  (schema-drift-safe)
-- ============================================================================
-- HOW TO RUN:
--   1. افتح https://supabase.com/dashboard
--   2. اختر مشروعك (معرّف المشروع في backend/.env — غير مكتوب هنا)
--   3. اضغط SQL Editor (أيقونة 📝 في القائمة اليسرى)
--   4. اضغط "+ New query"
--   5. الصق هذا الملف كاملاً
--   6. اضغط Run (Ctrl+Enter)
--
-- NOTE: الـ backend يستخدم service_role key الذي يتجاوز RLS تلقائياً، فلن يتأثر.
-- هذه طبقة دفاع إضافية لو تسرّب المفتاح يوماً.
--
-- لماذا DO block؟ المخطط الفعلي في Supabase يختلف عن create_tables.sql:
--   • messages ليس فيه company_id (يرتبط بالشركة عبر sender_id → users)
--   • training_records غير موجود إطلاقاً
--   • كل أعمدة الـ id نوعها TEXT (id TEXT DEFAULT gen_random_uuid()::text)،
--     لذلك نقارن نصّاً بنص بدون تحويل ::uuid (التحويل كان يسبّب
--     "operator does not exist: text = uuid").
-- النسخة أدناه تفحص وجود كل جدول + العمود المطلوب قبل تطبيق الـ policy،
-- فتتخطّى أي ناقص مع رسالة NOTICE بدل أن تتوقّف بخطأ 42703 / 42P01.
-- ============================================================================

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT * FROM (VALUES
      -- جداول تملك company_id مباشرةً
      ('crew',           'company_isolation_crew',          'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      ('flights',        'company_isolation_flights',       'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      ('notifications',  'company_isolation_notifications', 'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      ('audit_log',      'company_isolation_audit',         'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      ('aircraft',       'company_isolation_aircraft',      'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      ('routes',         'company_isolation_routes',        'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      ('settings',       'company_isolation_settings',      'company_id',
        'company_id = (auth.jwt() ->> ''company_id'')'),
      -- جداول ترث company_id من crew (عبر crew_id)
      ('documents',      'company_isolation_documents',     'crew_id',
        'crew_id IN (SELECT id FROM crew WHERE company_id = (auth.jwt() ->> ''company_id''))'),
      ('leave_requests', 'company_isolation_leave',         'crew_id',
        'crew_id IN (SELECT id FROM crew WHERE company_id = (auth.jwt() ->> ''company_id''))'),
      -- جدول يرث company_id من flights (عبر flight_id)
      ('assignments',    'company_isolation_assignments',   'flight_id',
        'flight_id IN (SELECT id FROM flights WHERE company_id = (auth.jwt() ->> ''company_id''))'),
      -- messages: لا company_id — العزل عبر sender_id → users
      ('messages',       'company_isolation_messages',      'sender_id',
        'sender_id IN (SELECT id FROM users WHERE company_id = (auth.jwt() ->> ''company_id''))')
    ) AS t(tbl, pol, reqcol, using_expr)
  LOOP
    -- الجدول موجود؟
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = r.tbl
    ) THEN
      RAISE NOTICE 'SKIP %  — الجدول غير موجود', r.tbl;
      CONTINUE;
    END IF;

    -- العمود المطلوب موجود؟
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = r.tbl AND column_name = r.reqcol
    ) THEN
      RAISE NOTICE 'SKIP %  — العمود % غير موجود', r.tbl, r.reqcol;
      CONTINUE;
    END IF;

    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', r.tbl);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', r.pol, r.tbl);
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL TO authenticated USING (%s)',
      r.pol, r.tbl, r.using_expr
    );
    RAISE NOTICE 'OK   %  — تم تفعيل RLS + policy %', r.tbl, r.pol;
  END LOOP;
END $$;

-- ── التحقق ────────────────────────────────────────────────────────────────
-- 1) الجداول التي فُعِّل عليها RLS
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public' AND rowsecurity = true
ORDER BY tablename;

-- 2) كل الـ policies المُنشأة
SELECT schemaname, tablename, policyname
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;
