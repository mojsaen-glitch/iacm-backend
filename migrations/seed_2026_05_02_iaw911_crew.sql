-- ─────────────────────────────────────────────────────────────────────
-- SEED: General Declaration crew — flight IAW 911-912
--   Aircraft : B737-800 (YI-ASU)   Routing: BSR-EBL-BSR   Date: 2026-05-02
--
-- Inserts the 12 crew RECORDS ONLY (no login accounts / no passwords).
-- After running this, create each person's login from their crew profile
-- via the «حساب الدخول» button — that keeps credential generation inside the
-- app's secure flow (users.crew_id is linked there).
--
-- Roles use the GenDec-aligned values (crew.rank):
--   CAPT → pilot_captain · F/O → pilot_first_officer · AME →
--   aircraft_maintenance_engineer · SCC → senior_cabin_crew · CC → cabin_crew ·
--   L/SH → load_sheet_officer · IFSO → in_flight_security_officer.
--   Flight deck + cabin are counted aircraft crew (full compliance); AME, L/SH
--   and IFSO are operational links only (light check, never counted).
--
-- Notes / assumptions (adjust before running if needed):
--   • employee_id = 911001..911012 (auto sequence; no official numbers given).
--   • full_name_ar = transliteration — replace with official Arabic if available.
--   • base = 'BSR' (Basra-based per the routing). Change to 'BGW' if needed.
--   • date_of_birth left NULL (fill later when editing the crew if required).
--   • aircraft_qualifications = 'B737' for flight deck + cabin; NULL for
--     operational roles (no aircraft type rating, never counted).
--
-- Run ONCE in the Supabase SQL Editor. Idempotent via ON CONFLICT (employee_id).
-- ─────────────────────────────────────────────────────────────────────

INSERT INTO crew
  (employee_id, full_name_ar, full_name_en, roster_name,
   company_id, base, rank, aircraft_qualifications, status, join_date)
VALUES
  ('911001','مصطفى أحمد عبدالكريم نورس','MUSTAFA AHMED ABDULKAREEM NAWRES','M.NAWRES',
     'iraqi_airways','BSR','pilot_captain','B737','active','2026-05-02'),
  ('911002','سلطان قحطان عدنان','SULTAN QAHTAN ADNAN','S.ADNAN',
     'iraqi_airways','BSR','pilot_first_officer','B737','active','2026-05-02'),
  ('911003','عمر مؤيد محمود','OMAR MUAYAD MAHMOOD','O.MAHMOOD',
     'iraqi_airways','BSR','aircraft_maintenance_engineer',NULL,'active','2026-05-02'),
  ('911004','ديالى علي حسين التميمي','DIYALA ALI HUSSEIN AL-TAMEEMI','D.ALTAMEEMI',
     'iraqi_airways','BSR','senior_cabin_crew','B737','active','2026-05-02'),
  ('911005','يسر قاسم خريبط خريبط','YUSUR QASIM KHRAIBET KHRAIBET','Y.KHRAIBET',
     'iraqi_airways','BSR','cabin_crew','B737','active','2026-05-02'),
  ('911006','علي محسن سلمان','ALI MOHSIN SALMAN','A.SALMAN',
     'iraqi_airways','BSR','cabin_crew','B737','active','2026-05-02'),
  ('911007','شروق قيس قاسم','SHUROOQ QAYS QASIM','SH.QASIM',
     'iraqi_airways','BSR','cabin_crew','B737','active','2026-05-02'),
  ('911008','رؤى عماد كريم','RUAA IMAD KAREEM','R.KAREEM',
     'iraqi_airways','BSR','cabin_crew','B737','active','2026-05-02'),
  ('911009','حسين غانم طعمة الدراجي','HUSSEIN GHANIM TUAMA ALDARRAJI','H.ALDARRAJI',
     'iraqi_airways','BSR','load_sheet_officer',NULL,'active','2026-05-02'),
  ('911010','محمد علي عبدالستار','MOHAMMED ALI A.A.SATTAR','M.SATTAR',
     'iraqi_airways','BSR','in_flight_security_officer',NULL,'active','2026-05-02'),
  ('911011','علي جمال منصور','ALI JAMAL MANSOOR','A.MANSOOR',
     'iraqi_airways','BSR','in_flight_security_officer',NULL,'active','2026-05-02'),
  ('911012','عباس سعد عنين الحلايجي','ABBAS SAAD ANAIN ALHLAICHI','A.ALHLAICHI',
     'iraqi_airways','BSR','in_flight_security_officer',NULL,'active','2026-05-02')
ON CONFLICT (employee_id) DO NOTHING;
