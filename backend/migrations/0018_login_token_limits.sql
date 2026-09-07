-- Indexes for the two questions the login rate limiter asks, and for the
-- cleanup job that finally stops this table growing for ever.
--
-- Until now the only email that left this system was a log line, so
-- /auth/login/request could be called without limit and cost nothing. It now
-- sends real mail on a metered account, which makes an unauthenticated
-- endpoint that spends money and mails strangers. The limiter counts rows
-- that are already here rather than inventing a table: a request that
-- succeeds commits its login_tokens row, so the committed rows ARE the
-- record of what was sent, and a refusal correctly costs nothing and records
-- nothing.
--
-- Two counts, two indexes:
--
--   * per address, "how many links has this inbox been sent lately" —
--     (email, created_at). It replaces 0017's (email) index rather than
--     joining it: email leads both, so the wider one still answers erasure's
--     DELETE ... WHERE email = %s, and keeping both would be one index to
--     maintain for nothing.
--   * across everyone, "how many have we sent today" — (created_at), which
--     the cleanup job's DELETE ... WHERE created_at < %s also rides on.
CREATE INDEX login_tokens_email_created_idx ON login_tokens (email, created_at DESC);
DROP INDEX login_tokens_email_idx;

CREATE INDEX login_tokens_created_at_idx ON login_tokens (created_at);

COMMENT ON INDEX login_tokens_email_created_idx IS
    'Per-address login rate limit, and erasure''s delete by email.';
COMMENT ON INDEX login_tokens_created_at_idx IS
    'The daily send cap, and the job that deletes tokens past retention.';
