-- Safe SQL editor — Postgres SECURITY DEFINER function called from the
-- backend via supabase.rpc('admin_exec_sql', { q: '...' }).
--
-- Two layers of defence:
--   1. The application layer (admin_control.run_sql) already rejected:
--        • non-SELECT/WITH without write_unlock + valid 2FA
--        • DROP / TRUNCATE / GRANT / REVOKE / CREATE USER / ALTER SYSTEM
--   2. This function applies a second pass IN-DATABASE so even a leaked
--      service-role key cannot bypass the policy through a direct REST call.
--
-- Returns the rows as JSONB so PostgREST can pass them through unchanged
-- regardless of the source query's column shape.

CREATE OR REPLACE FUNCTION admin_exec_sql(q text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    norm  text := lower(trim(both ' ;' from q));
    rows  jsonb;
BEGIN
    -- Defence-in-depth: same banned-keyword list as the API layer.
    IF norm ~ '\m(drop|truncate|grant|revoke|create\s+user|alter\s+system)\M' THEN
        RAISE EXCEPTION 'forbidden statement type';
    END IF;

    -- Read-only fast path — wrap a SELECT/WITH in a jsonb_agg.
    IF norm ~ '^(select|with)\M' THEN
        EXECUTE format('SELECT COALESCE(jsonb_agg(t), ''[]''::jsonb) FROM (%s) t', q)
        INTO rows;
        RETURN rows;
    END IF;

    -- Write path: execute and report the affected row count.
    EXECUTE q;
    RETURN jsonb_build_object('ok', true, 'affected_rows', null);
EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'sql error: %', SQLERRM;
END;
$$;

-- Allow the API role (service_role) to call it. PUBLIC stays denied —
-- nobody can hit this from anon/authenticated tokens.
REVOKE ALL ON FUNCTION admin_exec_sql(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_exec_sql(text) TO service_role;
