-- ============================================================================
-- IACM — Performance indexes for the hot, frequently-polled query paths.
-- ============================================================================
-- HOW TO RUN:
--   Supabase Dashboard → SQL Editor → New query → paste all → Run.
--
-- Safe to re-run (idempotent): each index is created only if its table AND all
-- its columns exist, and CREATE INDEX IF NOT EXISTS skips already-present ones.
-- This guards against schema drift between create_tables.sql and the live DB
-- (e.g. notifications uses `user_id` live even though the старый schema file
-- shows `target_user_id`).
--
-- Why these: the Flutter clients poll notifications/messages/flights on timers,
-- so these columns are read hundreds of times per second at scale. Without
-- indexes every poll is a full-table scan — the #1 cause of DB melt-down.
-- ============================================================================

DO $$
DECLARE
  r RECORD;
  cols_exist BOOLEAN;
  c TEXT;
BEGIN
  FOR r IN
    SELECT * FROM (VALUES
      -- [index_name, table, comma-separated columns, optional WHERE/order suffix]
      ('idx_notifications_user_unread', 'notifications', ARRAY['user_id','is_read','created_at']),
      ('idx_notifications_company',     'notifications', ARRAY['company_id']),
      ('idx_messages_receiver',         'messages',      ARRAY['receiver_id','is_read']),
      ('idx_messages_pair',             'messages',      ARRAY['sender_id','receiver_id','created_at']),
      ('idx_flights_company_pub',       'flights',       ARRAY['company_id','publish_status','status']),
      ('idx_flights_company_dep',       'flights',       ARRAY['company_id','departure_time']),
      ('idx_assignments_flight',        'assignments',   ARRAY['flight_id']),
      ('idx_assignments_crew',          'assignments',   ARRAY['crew_id']),
      ('idx_users_crew',                'users',         ARRAY['crew_id']),
      ('idx_users_company_active',      'users',         ARRAY['company_id','is_active'])
    ) AS t(idx, tbl, cols)
  LOOP
    -- table exists?
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = r.tbl
    ) THEN
      RAISE NOTICE 'SKIP %  — table % missing', r.idx, r.tbl;
      CONTINUE;
    END IF;

    -- all columns exist?
    cols_exist := TRUE;
    FOREACH c IN ARRAY r.cols LOOP
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = r.tbl AND column_name = c
      ) THEN
        cols_exist := FALSE;
        RAISE NOTICE 'SKIP %  — column %.% missing', r.idx, r.tbl, c;
        EXIT;
      END IF;
    END LOOP;
    IF NOT cols_exist THEN
      CONTINUE;
    END IF;

    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS %I ON %I (%s)',
      r.idx, r.tbl, array_to_string(r.cols, ', ')
    );
    RAISE NOTICE 'OK   %  on %(%)', r.idx, r.tbl, array_to_string(r.cols, ', ');
  END LOOP;
END $$;

-- Verify
SELECT tablename, indexname
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname LIKE 'idx_%'
ORDER BY tablename, indexname;
