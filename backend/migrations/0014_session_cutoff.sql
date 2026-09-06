-- "Sign out everywhere", and the answer to a stolen session token.
--
-- Sessions are stateless JWTs (identity.service): nothing server-side records
-- which ones exist, so until now nothing could revoke one short of rotating
-- the signing secret and signing every user out. This column is the cheap
-- middle: a per-user instant before which no session is accepted. Every
-- token now carries its issue time (iat), and the bearer gate's existing
-- per-request user lookup reads this column alongside deleted_at — still one
-- read per request.
--
-- Set by revoke_sessions (POST /auth/sessions/revoke) and by erasure. Whole
-- seconds, because iat is whole seconds: a token issued in the same second
-- as the cutoff still passes, which is the right side to err on for a user
-- signing straight back in.

ALTER TABLE users ADD COLUMN sessions_invalid_before timestamptz;

COMMENT ON COLUMN users.sessions_invalid_before IS
    'Sessions issued (iat) before this instant are refused. NULL = no cutoff.';
