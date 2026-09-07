-- Attachments on a forwarded ticket email.
--
-- Why this exists now, before anything reads it: every operator's Delay
-- Repay form requires a picture of the ticket, and until now nothing kept
-- one. `inbound_emails` stored sender, subject and body; the PDF or the
-- e-ticket image the whole email was built around was dropped on the floor.
-- A journey ingested without its attachment can never be filed, so the
-- attachment has to start being kept BEFORE the filing code exists, or
-- every ticket taken in until then is unclaimable.
--
-- Bytea, not object storage. claim_proofs.s3_key (0011) points at a bucket
-- that was never created, and for the traffic this system actually sees —
-- a few tickets a month, a couple of hundred kilobytes each — a bucket is
-- an account, a credential, a lifecycle policy and a second failure mode
-- for no gain. Postgres stores it, backs it up and erases it with the row
-- it belongs to. If volume ever makes that wrong, `content` becomes a key
-- and this table keeps its shape.
--
-- Size is bounded by the writer, not here: journeys.intake caps how big one
-- attachment may be and how many are kept per email, because a CHECK
-- constraint would turn a too-large attachment into a failed transaction
-- that loses the whole email instead of just that file.
--
-- PII: an e-ticket carries the passenger's name and a booking reference.
-- Erasure needs no new statement — journeys' erase deletes the user's
-- inbound_emails rows, and the CASCADE below takes the attachments with
-- them. Retention DOES need one: bodies are blanked in place rather than
-- deleted (0015), so the attachments beside them must be deleted at the
-- same moment or they would outlive the body they came with.

CREATE TABLE inbound_email_attachments (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    inbound_email_id uuid        NOT NULL REFERENCES inbound_emails (id) ON DELETE CASCADE,
    -- As the sending mail client named it, already stripped of any path by
    -- the reader. Only ever shown to the user or attached to a claim form;
    -- nothing opens it by name.
    filename         text        NOT NULL,
    -- The declared MIME type, lowercased. Declared, not sniffed: it decides
    -- what we KEEP, never what we execute.
    content_type     text        NOT NULL,
    content          bytea       NOT NULL,
    -- Denormalised from length(content) so a listing can show sizes without
    -- reading megabytes off disk to count them.
    size_bytes       integer     NOT NULL CHECK (size_bytes >= 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Every read is "the attachments of this email", and retention deletes by
-- the same key.
CREATE INDEX inbound_email_attachments_email_idx
    ON inbound_email_attachments (inbound_email_id);

COMMENT ON TABLE inbound_email_attachments IS
    'Files that arrived on a forwarded ticket email — the e-ticket PDF or '
    'image a Delay Repay claim has to be filed with. Deleted with the email '
    'row on erasure (CASCADE) and by the retention sweep when its body is '
    'blanked.';
COMMENT ON COLUMN inbound_email_attachments.content_type IS
    'MIME type as the sender declared it, lowercased. Used to decide what is '
    'worth keeping; never to decide how to handle the bytes.';
