-- The index erasure needs (0012 shipped without it).
--
-- login_tokens is written on every magic-link request and read back by
-- token_hash, which its UNIQUE constraint already indexes. But erasure asks
-- a different question — identity.repository._DELETE_LOGIN_TOKENS is
-- "DELETE FROM login_tokens WHERE email = %s" — and no index answered it,
-- so deleting an account meant a sequential scan of every login token ever
-- minted. Nothing prunes this table yet, so it only grows: the scan gets
-- slower for the rest of the system's life, and it happens inside the one
-- request a user is legally entitled to have honoured promptly.
--
-- Not partial. A spent or expired token is still that person's row and
-- erasure must remove it too, so there is no subset worth excluding.
CREATE INDEX login_tokens_email_idx ON login_tokens (email);

COMMENT ON INDEX login_tokens_email_idx IS
    'Erasure deletes a person''s pending and spent login tokens by email.';
