-- =============================================================
-- Device Tokens table — for FCM push notifications
-- Apply via Supabase SQL editor. Idempotent (re-runnable).
--
-- Note: `users.id` in this project is TEXT (not Supabase Auth UUID), so
-- we match that type here. The backend runs with the service-role key
-- which bypasses RLS — the policies below are a defence-in-depth measure
-- (deny-by-default for any client that ever talks to Supabase directly).
-- =============================================================

CREATE TABLE IF NOT EXISTS public.device_tokens (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    token         TEXT NOT NULL,
    platform      TEXT NOT NULL CHECK (platform IN ('android', 'ios', 'web', 'windows')),
    app_version   TEXT,
    device_name   TEXT,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A token is globally unique — if it migrates to a new user (rare),
    -- the new INSERT/UPSERT will overwrite the row.
    CONSTRAINT device_tokens_token_unique UNIQUE (token)
);

CREATE INDEX IF NOT EXISTS idx_device_tokens_user_id
    ON public.device_tokens (user_id);

CREATE INDEX IF NOT EXISTS idx_device_tokens_last_seen
    ON public.device_tokens (last_seen_at DESC);

-- RLS: deny-by-default. The backend uses the service-role key (bypasses
-- RLS by design), so the app stays functional. Any other client that
-- somehow reaches this table gets nothing.
ALTER TABLE public.device_tokens ENABLE ROW LEVEL SECURITY;

-- Drop any old policies from earlier attempts so the migration is re-runnable.
DROP POLICY IF EXISTS "users_own_device_tokens_select" ON public.device_tokens;
DROP POLICY IF EXISTS "users_own_device_tokens_insert" ON public.device_tokens;
DROP POLICY IF EXISTS "users_own_device_tokens_delete" ON public.device_tokens;
DROP POLICY IF EXISTS "device_tokens_service_role_only" ON public.device_tokens;

-- Explicit service-role-only policy — clearer than no policy at all.
CREATE POLICY "device_tokens_service_role_only"
    ON public.device_tokens
    AS PERMISSIVE
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

COMMENT ON TABLE public.device_tokens IS
    'FCM/APNs tokens registered by mobile clients for push notifications. '
    'Accessed only via the backend (service-role); RLS denies all other clients.';
