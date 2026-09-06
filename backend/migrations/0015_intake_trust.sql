-- Trust at the intake door, and how long a forwarded email's body is kept
-- (ARCHITECTURE §6a, "Trust at the door"). Three columns on inbound_emails:
--
--   * spf_pass / dkim_pass — what the mail provider said about the sender's
--     authentication, when it said anything. NULL is "not checked": the
--     provider adapter that carries these is not written yet, and a row
--     that arrived before it must not read as failed. An explicit false
--     refuses the email at arrival (journeys.intake.screen).
--   * body_purged_at — when the raw body was blanked by the retention job.
--     Bodies hold names, booking references and travel patterns; they are
--     kept only as long as a bad read might need re-running (the claim
--     window plus a buffer), then dropped. The reader's structured answer
--     (extraction) stays: it is what a reviewer looks at, and it names no
--     one.

ALTER TABLE inbound_emails
    ADD COLUMN spf_pass boolean,
    ADD COLUMN dkim_pass boolean,
    ADD COLUMN body_purged_at timestamptz;

-- The retention job's work queue: decided rows whose body is still held,
-- oldest decision first. Partial, so it shrinks as bodies are purged and
-- never holds the rows still waiting to be read.
CREATE INDEX inbound_emails_retention_idx ON inbound_emails (processed_at)
    WHERE processed_at IS NOT NULL AND body_purged_at IS NULL;

COMMENT ON COLUMN inbound_emails.spf_pass IS
    'The provider''s SPF verdict on the sender. NULL when it gave none.';
COMMENT ON COLUMN inbound_emails.dkim_pass IS
    'The provider''s DKIM verdict on the sender. NULL when it gave none.';
COMMENT ON COLUMN inbound_emails.body_purged_at IS
    'When the retention job blanked body. NULL while the body is still held.';
