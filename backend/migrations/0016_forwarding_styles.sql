-- Both ways a person forwards a ticket email, and the setup message Gmail
-- sends before either can work (ARCHITECTURE §6a, "Trust at the door").
--
-- 0015 assumed one shape: the account holder presses Forward, so the From
-- header is them. That is only the MANUAL case. When a user sets up a Gmail
-- forwarding RULE, Gmail redirects the retailer's original message
-- untouched: the From header still says the ticket seller, and the only
-- headers naming the user are ones any sender can type. So the rule
-- "From must equal the account email" rejected every automatically
-- forwarded ticket — the case the product is actually for.
--
--   * forwarding — which shape arrived. 'manual' means the From address was
--     the account holder's own, which Gmail only produces when a person
--     presses Forward. 'automatic' means it was not, which is what a
--     forwarding rule produces, and also what a stranger would produce. The
--     column records the evidence; it does not pretend to prove identity.
--     NULL for mail that never got that far (an unknown recipient).
--
--   * status 'confirmation' — Gmail will not start forwarding until a code
--     it emails to the destination is entered back into Gmail. That email
--     lands here, from Google, and it is not a ticket. Without this status
--     the reader would see it, answer "not a ticket", and the row would be
--     filed as rejected — so the user would never see the code and could
--     never finish setting forwarding up. The code goes in status_reason,
--     which the settings page shows.
--
-- What this migration deliberately does NOT do is restore a sender gate in
-- another form. Nothing in an automatically forwarded message proves who
-- forwarded it, so the forwarding address is the credential: it is minted
-- at 128 bits, it can be replaced, and the per-user daily cap still bounds
-- what any one address can spend.

ALTER TABLE inbound_emails
    ADD COLUMN forwarding text CHECK (forwarding IN ('manual', 'automatic'));

ALTER TABLE inbound_emails
    DROP CONSTRAINT inbound_emails_status_check;

ALTER TABLE inbound_emails
    ADD CONSTRAINT inbound_emails_status_check CHECK (
        status IN ('received', 'parsed', 'needs_review', 'rejected',
                   'duplicate', 'failed', 'confirmation')
    );

COMMENT ON COLUMN inbound_emails.forwarding IS
    'manual = the From address was the account holder (they pressed Forward); '
    'automatic = it was not (a forwarding rule, or a stranger). Evidence, not proof.';
