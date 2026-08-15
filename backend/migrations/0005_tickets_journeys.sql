-- Tickets and journeys — the core of the product.
--
-- A TICKET is what the user bought (proof of a right to travel, with a price).
-- A JOURNEY is one dated leg we monitor for delay. A single ticket produces one
-- journey (MVP), two (a return), or several later (split ticketing) — hence
-- journeys reference tickets many-to-one, not one-to-one.
--
-- Design notes:
--   * raw ingest is separated from parsed data: tickets.source_payload keeps
--     whatever arrived (barcode text, email body reference) so a bad parse can
--     be re-run without asking the user again. Parsed fields live in columns.
--   * price_pence integer, never a float. VALUE IS PENCE.
--   * retailer (who sold it) is free text; operator_id on the JOURNEY (who ran
--     the train, who pays the claim) references operators. These genuinely
--     differ — a Trainline ticket on a GWR service is claimed from GWR.
--   * Timestamps are timestamptz (UTC on the wire). travel_date is a date in
--     UK terms, stored explicitly rather than derived from the departure
--     instant, because "which service was this" is defined by the UK timetable
--     day — deriving it through a timezone conversion breaks on BST edges.
--   * journeys is NOT partitioned yet. ARCHITECTURE §4 plans monthly
--     partitioning by travel_date at scale; doing it now would force travel_date
--     into every PK and FK for zero benefit at current volume. The rung-3
--     migration will do it with a copy-swap. Indexes are chosen so that the
--     access paths already group by travel_date.
--   * Darwin identifiers: rid (RTTI id, unique per service per day) is the
--     stable join key to the live feed; uid+travel_date identifies the
--     timetabled service. Both nullable — manual-entry journeys may match later.

CREATE TABLE tickets (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind            text        NOT NULL DEFAULT 'single'
                                CHECK (kind IN ('single', 'return', 'season')),
    price_pence     integer     NOT NULL CHECK (price_pence >= 0),
    retailer        text,
    -- How it entered the system, and the raw material to re-parse.
    source          text        NOT NULL
                                CHECK (source IN ('barcode', 'manual', 'email')),
    source_payload  text,
    purchased_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tickets_user_id_idx ON tickets (user_id);

CREATE TRIGGER tickets_set_updated_at
    BEFORE UPDATE ON tickets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE tickets IS
    'What the user bought. Price in PENCE. source_payload preserves the raw '
    'ingest (barcode text / email reference) so parsing is repeatable.';
COMMENT ON COLUMN tickets.retailer IS
    'Who SOLD the ticket (free text: "Trainline", "GWR app"...). The operator '
    'who PAYS a claim lives on the journey, not here.';

CREATE TABLE journeys (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    ticket_id           uuid        NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    operator_id         uuid        REFERENCES operators (id),

    origin_crs          text        NOT NULL CHECK (origin_crs ~ '^[A-Z]{3}$'),
    destination_crs     text        NOT NULL CHECK (destination_crs ~ '^[A-Z]{3}$'),
    travel_date         date        NOT NULL,
    scheduled_departure timestamptz NOT NULL,
    scheduled_arrival   timestamptz NOT NULL,

    -- Darwin/RTTI identifiers, filled in when we match the journey to a service.
    darwin_rid          text,
    darwin_uid          text,

    -- 'pending'  : not yet matched to a timetabled service
    -- 'matched'  : matched; being monitored (stream + nightly sweep)
    -- 'assessed' : actual arrival known; delay decision made (see delay_detections)
    -- 'unmatched': we gave up matching (bad data, service not found)
    status              text        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN
                                      ('pending', 'matched', 'assessed', 'unmatched')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT journeys_times_ordered CHECK (scheduled_arrival > scheduled_departure),
    -- One row per ticket per travelled leg of a given service day. Guards the
    -- return-ticket case (same ticket, out + back = different dates/directions)
    -- while blocking accidental double-adds.
    CONSTRAINT journeys_ticket_leg_key UNIQUE (ticket_id, travel_date, origin_crs, destination_crs)
);

-- The two hot paths:
--   API: this user's journeys, newest first.
CREATE INDEX journeys_user_date_idx ON journeys (user_id, travel_date DESC);
--   Sweep/worker: monitored journeys for a service day still awaiting assessment.
CREATE INDEX journeys_monitor_idx ON journeys (travel_date, status)
    WHERE status IN ('pending', 'matched');
--   Ingestor: match an incoming Darwin movement to journeys by rid.
CREATE INDEX journeys_darwin_rid_idx ON journeys (darwin_rid) WHERE darwin_rid IS NOT NULL;

CREATE TRIGGER journeys_set_updated_at
    BEFORE UPDATE ON journeys
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE journeys IS
    'One dated leg being monitored for delay. The unit the delay engine and '
    'claims operate on. Partitioning by travel_date is deferred to rung 3.';
COMMENT ON COLUMN journeys.travel_date IS
    'UK timetable day, stored explicitly (NOT derived from scheduled_departure: '
    'timezone conversion around BST would misassign late-evening services).';
COMMENT ON COLUMN journeys.darwin_rid IS
    'RTTI id of the matched service — the join key for Darwin Push Port messages.';
