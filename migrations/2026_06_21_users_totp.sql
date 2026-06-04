-- 2FA TOTP support — plan §8.1 makes 2FA mandatory for any /admin/* access.
--
-- We extend the existing `users` table rather than create a side table:
--   • totp_secret    — base32, hex-encrypted-at-rest server-side (the column
--                      itself stores the cleartext base32; rotate via
--                      Supabase column encryption later if needed).
--   • totp_enabled   — false until the user verifies a code from the QR scan.
--   • totp_enrolled_at — auditing.
--
-- A user with totp_enabled=true must pass a code on every login. The
-- backend implementation is in app/api/v1/endpoints/auth.py (login flow).

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS totp_secret      TEXT,
    ADD COLUMN IF NOT EXISTS totp_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS totp_enrolled_at TIMESTAMPTZ;

-- Index so the login path can do a fast existence check on enabled 2FA
-- without a full users scan. Partial — only ~1% of rows will have it set.
CREATE INDEX IF NOT EXISTS users_totp_enabled_idx
    ON users (id) WHERE totp_enabled = TRUE;
