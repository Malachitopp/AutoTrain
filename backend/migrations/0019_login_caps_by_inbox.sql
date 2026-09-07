-- Two holes in the login caps (0018), both found by adversarial review
-- before anything shipped, closed here together.
--
-- The per-address cap keyed on the address as typed. Nearly every provider
-- delivers victim+anything@example.com to victim@example.com, so changing
-- the suffix bought a fresh five-a-day budget for the same inbox every
-- time — the cap that exists to stop one person being mail-bombed did not
-- stop it. login_tokens.email_key is the address reduced to the inbox it
-- reaches (lower-cased, plus-suffix stripped; identity.service does the
-- reduction) and the count keys on it. email stays the address as typed,
-- because that is where the mail goes. The trade is deliberate: a few
-- providers treat '+' as an ordinary character, and there two genuinely
-- different mailboxes will share one budget. Sharing is the safe side.
--
-- The daily cap counted login_tokens, and erasure deletes login_tokens.
-- So: request five links, sign in, delete the account, and the day's
-- count fell by five — an authenticated loop that reached the provider's
-- quota without ever meeting our cap. login_sends records that a login
-- email went out and nothing else: no address, no token, no user. It is
-- not personal data, so erasure has no reason to touch it, and the count
-- is beyond anyone's reach.
--
-- Erasure now deletes by email_key. A person forgotten as a@example.com is
-- the person who received the links sent to a+shop@example.com; those rows
-- carry their address and go with them. 0018's (email, created_at) index
-- has no reader left and is replaced rather than joined.

ALTER TABLE login_tokens ADD COLUMN email_key citext;
UPDATE login_tokens SET email_key = lower(regexp_replace(email::text, '\+[^@]*@', '@'));
ALTER TABLE login_tokens ALTER COLUMN email_key SET NOT NULL;

CREATE INDEX login_tokens_email_key_created_idx ON login_tokens (email_key, created_at DESC);
DROP INDEX login_tokens_email_created_idx;

CREATE TABLE login_sends (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_sends_created_at_idx ON login_sends (created_at);

COMMENT ON COLUMN login_tokens.email_key IS
    'The inbox the address reaches: lower-cased, plus-suffix stripped. The per-address cap and erasure key on it.';
COMMENT ON TABLE login_sends IS
    'One row per login email sent. No address and no token on purpose: the daily cap counts it, and erasure cannot lower it.';
COMMENT ON INDEX login_tokens_email_key_created_idx IS
    'Per-inbox login cap, and erasure''s delete by inbox.';
COMMENT ON INDEX login_sends_created_at_idx IS
    'The daily send cap, and the job that deletes sends past retention.';
