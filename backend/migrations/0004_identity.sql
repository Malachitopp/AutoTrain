-- Users, authentication and devices.
--
-- Design notes:
--   * email is citext (migration 0001): case-insensitive uniqueness at the
--     type level, so no LOWER() discipline needed anywhere.
--   * password_hash is nullable: accounts created via magic-link / OAuth have
--     no password. A CHECK ensures at least one auth route exists is NOT added
--     yet — OAuth identities arrive with their own table later, and until then
--     a NULL hash simply means "cannot log in with a password".
--   * deleted_at implements GDPR erasure as anonymise-not-delete: claims must
--     survive for audit/dispute (we filed things with operators on the user's
--     behalf), so erasure blanks PII columns and stamps deleted_at rather than
--     deleting rows. A partial unique index keeps dead emails reusable.
--   * consent: filing a claim on someone's behalf needs recorded authority
--     (PLAN.md §6). Stored as explicit columns, not a boolean — WHEN and WHAT
--     VERSION was consented to is the legally useful part.

CREATE TABLE users (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email               citext,
    password_hash       text,
    display_name        text,
    -- Letter-of-authority consent to file Delay Repay claims on their behalf.
    claim_consent_at    timestamptz,
    claim_consent_terms text,
    deleted_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- A live account must have an email; an erased one must not.
    CONSTRAINT users_email_presence CHECK (
        (deleted_at IS NULL AND email IS NOT NULL)
        OR (deleted_at IS NOT NULL AND email IS NULL)
    )
);

CREATE UNIQUE INDEX users_email_key ON users (email) WHERE deleted_at IS NULL;

CREATE TRIGGER users_set_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE users IS
    'Accounts. GDPR erasure anonymises (nulls PII, sets deleted_at) rather than '
    'deleting, because filed claims must remain auditable.';
COMMENT ON COLUMN users.claim_consent_at IS
    'When the user granted authority to file claims on their behalf. NULL = '
    'auto-filing disabled for this user; deep links still work.';
COMMENT ON COLUMN users.claim_consent_terms IS
    'Identifier/version of the consent wording they accepted, e.g. "loa-v1".';

CREATE TABLE devices (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    platform      text        NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    -- FCM/APNs registration token. Unique globally: a token identifies one
    -- device install, and re-registration under a new user must steal it.
    push_token    text        NOT NULL UNIQUE,
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX devices_user_id_idx ON devices (user_id);

COMMENT ON TABLE devices IS
    'Push notification targets. A user may have several; a token belongs to '
    'exactly one user (ON CONFLICT on push_token re-homes it at re-register).';
