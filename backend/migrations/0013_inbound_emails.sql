-- Ticket-email intake (ARCHITECTURE §6a): the user forwards a booking
-- confirmation to a personal AutoTrain address, and the journeys module
-- reads the journey out of it. Two pieces of state:
--
--   * users.forwarding_code — the personal part of that address
--     (tickets-<code>@<inbound domain>). Minted on first request, never
--     rotated in v1. Nullable: accounts that never open the settings card
--     never get one.
--   * inbound_emails — every email that arrived, raw, and what became of it.
--     The raw body is kept so a bad read can be re-run without asking the
--     user to forward again (0005's source_payload rule). tickets created
--     from an email point back here through tickets.source_payload.
--
-- Status is a work queue, in the house shape (a partial index over the
-- unprocessed rows, swept by the scheduler):
--   'received'     : stored, not yet read
--   'parsed'       : journeys created
--   'needs_review' : read, but not trusted enough to act on — the reason is
--                    in status_reason; a person (or a later, better reader)
--                    decides
--   'rejected'     : not a ticket at all (marketing, receipts), or a kind we
--                    do not monitor (season tickets)
--   'duplicate'    : the journeys it describes were already tracked
--   'failed'       : the reader kept erroring; attempts is the count
--
-- PII note: bodies hold names, booking references and travel patterns. GDPR
-- erasure (0004's anonymise-not-delete) must DELETE this user's rows here —
-- the CASCADE covers a hard delete, not the anonymising path.

ALTER TABLE users ADD COLUMN forwarding_code text;

CREATE UNIQUE INDEX users_forwarding_code_key ON users (forwarding_code)
    WHERE forwarding_code IS NOT NULL;

COMMENT ON COLUMN users.forwarding_code IS
    'Personal part of the ticket-forwarding address tickets-<code>@<domain>. '
    'Lowercase, unguessable, minted on first request. NULL until then.';

CREATE TABLE inbound_emails (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- NULL when the recipient address matched nobody: the row records that
    -- mail arrived for a code no account has, with the body dropped.
    user_id         uuid        REFERENCES users (id) ON DELETE CASCADE,
    -- The provider's Message-ID: the idempotency key for webhook retries.
    message_id      text        NOT NULL UNIQUE,
    sender          text        NOT NULL,
    recipient       text        NOT NULL,
    subject         text        NOT NULL DEFAULT '',
    body            text        NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),

    status          text        NOT NULL DEFAULT 'received'
                                CHECK (status IN ('received', 'parsed', 'needs_review',
                                                  'rejected', 'duplicate', 'failed')),
    -- Why it is where it is, in the reader's words; shown to the user.
    status_reason   text,
    -- The reader's structured answer, kept whatever the outcome: it is what a
    -- reviewer looks at, and what a re-run is compared against.
    extraction      jsonb,
    attempts        integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    processed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- A row that has been decided says when; one still queued does not.
    CONSTRAINT inbound_emails_processed_when_decided CHECK (
        (status = 'received') = (processed_at IS NULL)
    )
);

-- The intake sweep's work queue: oldest unread first. Partial, so it stays
-- the size of the backlog, not the table.
CREATE INDEX inbound_emails_queue_idx ON inbound_emails (received_at)
    WHERE status = 'received';

-- The user's view: what happened to the emails they forwarded, newest first.
CREATE INDEX inbound_emails_user_idx ON inbound_emails (user_id, received_at DESC);

CREATE TRIGGER inbound_emails_set_updated_at
    BEFORE UPDATE ON inbound_emails
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE inbound_emails IS
    'Forwarded ticket emails, raw, and what the reader made of each. '
    'status = received is the intake sweep''s work queue.';
COMMENT ON COLUMN inbound_emails.extraction IS
    'The structured reading (journeys.intake.ExtractedTicket as JSON), kept for '
    'review and re-runs whatever the status.';
