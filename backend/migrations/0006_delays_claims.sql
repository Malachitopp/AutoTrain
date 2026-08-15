-- Delay detections and claims.
--
-- The idempotency spine of the system. Two independent sources (Darwin stream,
-- nightly HSP sweep) can both conclude "this journey was late"; at-least-once
-- queues can deliver the same message twice; a claim submission can time out
-- after succeeding. Every one of those is absorbed by a unique constraint here.
--
--   Guarantee 1: at most ONE delay detection per journey.
--     delay_detections.journey_id UNIQUE.
--     Writer: INSERT ... ON CONFLICT (journey_id) DO NOTHING; rowcount 0 means
--     someone got there first — stop, do not notify.
--
--   Guarantee 2: at most ONE claim per journey.
--     claims.journey_id UNIQUE. Same pattern.
--
--   Guarantee 3: notified exactly once.
--     notified_at on the detection, set by the worker in the same transaction
--     that sends the push. NULL = not yet notified; the worker's work-query is
--     WHERE notified_at IS NULL ... FOR UPDATE SKIP LOCKED, so two workers
--     cannot pick up the same row.
--
-- Design notes:
--   * A detection is superseded, never updated: if HSP later contradicts the
--     stream (rare, but the sweep is the backstop for a reason), the new truth
--     is written by UPDATE of actual_arrival/source + a fresh assessment, and
--     the audit of what we believed first lives in observed_* columns. We do
--     NOT allow a second detection row — one journey, one decision.
--   * entitlement_pence is computed AT DETECTION TIME from delay_repay_bands
--     and frozen here. Operators change their schemes; the claim must reflect
--     the scheme at time of travel, not at time of query.
--   * Claim states are a linear-ish machine kept in code, not triggers. The DB
--     enforces the set of states and appends every transition to
--     claim_events (INSERT-only) for audit; legality of a transition is the
--     service layer's job. Rationale: trigger-enforced state machines are
--     miserable to evolve, and the audit trail is what actually matters.
--   * submission_token: the idempotency key for form_submit adapters. Generated
--     once when the claim row is created; the adapter includes it in whatever
--     the operator flow allows (reference field / dedupe key). A retry after an
--     ambiguous timeout reuses the token, so the operator side can dedupe.

CREATE TABLE delay_detections (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    journey_id          uuid        NOT NULL UNIQUE REFERENCES journeys (id) ON DELETE CASCADE,

    actual_arrival      timestamptz NOT NULL,
    delay_minutes       integer     NOT NULL CHECK (delay_minutes >= 0),
    source              text        NOT NULL CHECK (source IN ('darwin', 'hsp')),
    observed_at         timestamptz NOT NULL DEFAULT now(),

    -- The frozen entitlement decision.
    band_percent        integer     CHECK (band_percent BETWEEN 0 AND 100),
    entitlement_pence   integer     NOT NULL DEFAULT 0 CHECK (entitlement_pence >= 0),

    notified_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Worker queue: unnotified qualifying delays.
CREATE INDEX delay_detections_pending_idx ON delay_detections (observed_at)
    WHERE notified_at IS NULL AND entitlement_pence > 0;

CREATE TRIGGER delay_detections_set_updated_at
    BEFORE UPDATE ON delay_detections
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE delay_detections IS
    'One per journey (UNIQUE journey_id — the dedupe point for stream vs sweep). '
    'entitlement_pence = 0 means "late but under threshold": recorded, never notified.';
COMMENT ON COLUMN delay_detections.entitlement_pence IS
    'Computed from delay_repay_bands at detection time and FROZEN. Pence.';
COMMENT ON COLUMN delay_detections.notified_at IS
    'Set in the same transaction as the push send. The exactly-once notification '
    'guarantee lives here (worker: WHERE notified_at IS NULL FOR UPDATE SKIP LOCKED).';

CREATE TABLE claims (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    journey_id          uuid        NOT NULL UNIQUE REFERENCES journeys (id) ON DELETE RESTRICT,
    detection_id        uuid        NOT NULL UNIQUE REFERENCES delay_detections (id) ON DELETE RESTRICT,
    operator_id         uuid        NOT NULL REFERENCES operators (id),
    user_id             uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,

    amount_pence        integer     NOT NULL CHECK (amount_pence > 0),
    -- 'draft'        : created, awaiting user action or auto-file
    -- 'ready'        : queued for the adapter
    -- 'submitted'    : adapter reports the operator accepted the submission
    -- 'needs_user'   : automation failed / unsupported; user given a deep link
    -- 'approved'     : operator approved (amount may differ from ours)
    -- 'rejected'     : operator rejected
    -- 'paid'         : user reports/confirms payment received
    -- 'expired'      : claim window passed without filing
    status              text        NOT NULL DEFAULT 'draft'
                                    CHECK (status IN ('draft', 'ready', 'submitted',
                                      'needs_user', 'approved', 'rejected', 'paid', 'expired')),
    -- Idempotency key for server-side submission retries.
    submission_token    uuid        NOT NULL DEFAULT gen_random_uuid(),
    file_by             date        NOT NULL,
    submitted_at        timestamptz,
    resolved_at         timestamptz,
    -- Operator's own reference for the claim, once known.
    operator_reference  text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Adapter work queue and deadline sweep.
CREATE INDEX claims_workable_idx ON claims (file_by)
    WHERE status IN ('draft', 'ready');
CREATE INDEX claims_user_idx ON claims (user_id, created_at DESC);

CREATE TRIGGER claims_set_updated_at
    BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE claims IS
    'One Delay Repay claim per journey (UNIQUE journey_id). ON DELETE RESTRICT '
    'from users: erase via anonymisation, never by dropping filed claims.';
COMMENT ON COLUMN claims.file_by IS
    'travel_date + operator.claim_window_days, frozen at creation. The deadline '
    'sweep expires claims past this date.';
COMMENT ON COLUMN claims.submission_token IS
    'Stable idempotency key across submission retries: a timeout that actually '
    'succeeded is deduped operator-side by this reference.';

CREATE TABLE claim_events (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id    uuid        NOT NULL REFERENCES claims (id) ON DELETE CASCADE,
    -- NULL from_status = creation event.
    from_status text,
    to_status   text        NOT NULL,
    -- Free-form context: adapter output, rejection reason, error detail.
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX claim_events_claim_idx ON claim_events (claim_id, created_at);

COMMENT ON TABLE claim_events IS
    'Append-only history of claim state transitions. Never updated or deleted; '
    'the audit trail for "what did we do on the user''s behalf, when".';

CREATE TABLE claim_proofs (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id    uuid        NOT NULL REFERENCES claims (id) ON DELETE CASCADE,
    kind        text        NOT NULL CHECK (kind IN ('ticket_image', 'hsp_record', 'email')),
    -- S3 object key (images/emails) or inline JSON (HSP API responses). One of.
    s3_key      text,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT claim_proofs_one_body CHECK (
        (s3_key IS NOT NULL AND payload IS NULL)
        OR (s3_key IS NULL AND payload IS NOT NULL)
    )
);

CREATE INDEX claim_proofs_claim_idx ON claim_proofs (claim_id);

COMMENT ON TABLE claim_proofs IS
    'Evidence attached to a claim: ticket image / HSP actual-arrival record. '
    'Binary data lives in S3; this table stores keys, or small JSON inline.';
