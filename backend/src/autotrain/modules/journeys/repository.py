"""SQL for the journeys module — the only file in the module that contains any.

Constants are deliberately unannotated: pyright then keeps their LiteralString
type, which is what lets `core.db` reject any statement assembled from runtime
values (ARCHITECTURE §3). Values travel as `%s` parameters, never in the text.

Every function takes the connection first and never commits — the caller owns
the transaction, which is how ticket + journey creation stays atomic without
either insert knowing about the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from autotrain.core import db
from autotrain.modules.journeys.models import (
    AssessableJourney,
    ClaimContext,
    InboundEmailRow,
    InboundEmailSummary,
    JourneyRow,
    NotificationContext,
)

# The full journeys column list is repeated verbatim in each statement below:
# core.db maps rows by name and treats an unexpected column as an error, so
# SELECT * never appears — and building the list by concatenation would trip
# both S608 and the LiteralString guarantee these constants exist to keep.

_INSERT_TICKET = (
    "INSERT INTO tickets (user_id, kind, price_pence, source, retailer, source_payload) "
    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id"
)

_INSERT_JOURNEY = (
    "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
    "travel_date, scheduled_departure, scheduled_arrival) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "RETURNING id, user_id, ticket_id, operator_id, origin_crs, destination_crs, "
    "travel_date, scheduled_departure, scheduled_arrival, "
    "darwin_rid, darwin_uid, status, created_at, updated_at"
)

# Ownership lives in the WHERE clause: a journey belonging to someone else is
# indistinguishable from one that does not exist.
_GET_FOR_USER = (
    "SELECT id, user_id, ticket_id, operator_id, origin_crs, destination_crs, "
    "travel_date, scheduled_departure, scheduled_arrival, "
    "darwin_rid, darwin_uid, status, created_at, updated_at "
    "FROM journeys WHERE id = %s AND user_id = %s"
)

# Newest travel day first — exactly the shape journeys_user_date_idx serves.
# created_at breaks ties so same-day journeys keep a stable order.
_LIST_FOR_USER = (
    "SELECT id, user_id, ticket_id, operator_id, origin_crs, destination_crs, "
    "travel_date, scheduled_departure, scheduled_arrival, "
    "darwin_rid, darwin_uid, status, created_at, updated_at "
    "FROM journeys WHERE user_id = %s "
    "ORDER BY travel_date DESC, created_at DESC LIMIT %s"
)

# Mirrors journeys_user_leg_departure_key (migration 0009) exactly: same leg,
# same day, DIFFERENT departure is a second genuine journey, not a duplicate.
_LEG_EXISTS = (
    "SELECT EXISTS (SELECT 1 FROM journeys WHERE user_id = %s AND travel_date = %s "
    "AND origin_crs = %s AND destination_crs = %s AND scheduled_departure = %s)"
)


# --- Inbound ticket emails (0013) ------------------------------------------

# The full inbound_emails column list, repeated verbatim for the same reasons
# as the journeys list above.

# ON CONFLICT (message_id) DO NOTHING: a webhook retry of an email already
# stored returns no row, and the caller reads "no row" as "seen before".
_INSERT_INBOUND_EMAIL = (
    "INSERT INTO inbound_emails (user_id, message_id, sender, recipient, subject, body, "
    "spf_pass, dkim_pass, forwarding, status, status_reason, processed_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (message_id) DO NOTHING "
    "RETURNING id, user_id, message_id, sender, recipient, subject, body, received_at, "
    "status, status_reason, extraction, attempts, processed_at, created_at, updated_at, "
    "spf_pass, dkim_pass, body_purged_at, forwarding"
)

# Which of these message ids are already stored. The mailbox poll asks this
# before downloading anything: the unique index on message_id would refuse a
# re-store anyway, but only after the whole body had crossed the network,
# and a mailbox is re-listed every interval over a window of days. The
# array_agg keeps it one scalar round trip.
_KNOWN_MESSAGE_IDS = (
    "SELECT coalesce(array_agg(message_id), '{}') FROM inbound_emails "
    "WHERE message_id = ANY(%s::text[])"
)

# Attachments (0020). size_bytes is written from the same bytes that go into
# content, so the two can never disagree.
_INSERT_ATTACHMENT = (
    "INSERT INTO inbound_email_attachments "
    "(inbound_email_id, filename, content_type, content, size_bytes) "
    "VALUES (%s, %s, %s, %s, %s)"
)

# Retention's companion (0020). Bodies are blanked in place rather than
# deleted, so the files beside them have to be removed at the same moment or
# they outlive the words that explain what they are. Batched on the same
# principle as the body purge, and idempotent: a second pass finds nothing.
_PURGE_PURGED_ATTACHMENTS = (
    "DELETE FROM inbound_email_attachments WHERE id IN ("
    "SELECT a.id FROM inbound_email_attachments a "
    "JOIN inbound_emails e ON e.id = a.inbound_email_id "
    "WHERE e.body_purged_at IS NOT NULL LIMIT %s)"
)

# The intake sweep's page: oldest unread first (inbound_emails_queue_idx).
# The caller passes the ids it has given up on for this sweep, so a raising
# email does not head every page until the sweep ends. Every 'received' row
# is listed, whatever its attempts: the sweep itself retires the exhausted
# ones (a crash on the last attempt must not strand a row). id breaks
# received_at ties so a page order is stable.
_LIST_RECEIVED_EMAILS = (
    "SELECT id, user_id, message_id, sender, recipient, subject, body, received_at, "
    "status, status_reason, extraction, attempts, processed_at, created_at, updated_at, "
    "spf_pass, dkim_pass, body_purged_at, forwarding "
    "FROM inbound_emails WHERE status = 'received' AND NOT (id = ANY(%s::uuid[])) "
    "ORDER BY received_at, id LIMIT %s"
)

# Six columns, not the row: the body and the reader's raw answer are not
# shown to the user, and the body is the largest column in the schema
# (models.InboundEmailSummary says why). inbound_emails_user_idx serves the
# WHERE and the ORDER BY either way.
_LIST_INBOUND_FOR_USER = (
    "SELECT id, received_at, sender, subject, status, status_reason "
    "FROM inbound_emails WHERE user_id = %s ORDER BY received_at DESC, id LIMIT %s"
)

# Guarded on status like the marks below: counting an attempt on a row
# another writer has already decided would be a second paid read of it.
_BUMP_ATTEMPTS = (
    "UPDATE inbound_emails SET attempts = attempts + 1 WHERE id = %s AND status = 'received'"
)

# Guarded on status: a decided row is never re-decided, so two sweeps reaching
# one email (or a sweep and a reviewer) cannot overwrite each other.
_MARK_PROCESSED = (
    "UPDATE inbound_emails SET status = %s, status_reason = %s, extraction = %s, "
    "processed_at = now() WHERE id = %s AND status = 'received'"
)

_MARK_FAILED_IF_EXHAUSTED = (
    "UPDATE inbound_emails SET status = 'failed', status_reason = %s, processed_at = now() "
    "WHERE id = %s AND status = 'received' AND attempts >= %s"
)

# The door's per-user count (intake.screen's daily cap). Two things it must
# not do:
#
# * count rows the door itself refused. Those cost no reader call, and
#   counting them means a flood keeps the window shut long after it stops —
#   the refusals hold the cap up by themselves, and the user never gets back
#   in. Confirmation messages are excluded for the same reason: recognised,
#   never read.
# * scan the whole window. The count is the check that bounds a flood, so it
#   must not itself grow with one; the inner LIMIT stops it at the cap, so
#   the work is the same whether the user has 51 emails today or a million.
_COUNT_RECEIVED_SINCE = (
    "SELECT count(*) FROM (SELECT 1 FROM inbound_emails "
    "WHERE user_id = %s AND received_at >= %s AND status NOT IN ('rejected', 'confirmation') "
    "LIMIT %s) AS capped"
)

# Retention (0015): blank the bodies of emails decided before an instant, a
# batch at a time on inbound_emails_retention_idx. Nothing else on the row
# changes; the structured reading stays.
#
# Its companion below covers the rows retention would otherwise never reach:
# an email nobody ever read (no extractor configured, or one that stayed
# broken) keeps status 'received' and a NULL processed_at for ever, so it is
# outside the retention index and its body would be held indefinitely. Past
# the window there is nothing useful left to read — the 28-day claim window
# closed long before — so the row is retired and blanked in one statement.
# The queue index (0013) serves the subquery.
_RETIRE_STALE_RECEIVED = (
    "UPDATE inbound_emails SET status = 'failed', status_reason = %s, "
    "processed_at = now(), body = '', body_purged_at = now() "
    "WHERE id IN (SELECT id FROM inbound_emails "
    "WHERE status = 'received' AND received_at < %s ORDER BY received_at LIMIT %s)"
)

_PURGE_OLD_BODIES = (
    "UPDATE inbound_emails SET body = '', body_purged_at = now() "
    "WHERE id IN (SELECT id FROM inbound_emails "
    "WHERE processed_at < %s AND body_purged_at IS NULL ORDER BY processed_at LIMIT %s)"
)

# --- Erasure (GDPR) ---------------------------------------------------------
# identity.erase_user asks each module to forget the person. Journeys keeps
# the journeys (claims reference them, RESTRICT) but drops the raw emails and
# the seller/email references on tickets — the only text here that came from
# the person rather than the timetable.
_DELETE_INBOUND_EMAILS = "DELETE FROM inbound_emails WHERE user_id = %s"
_STRIP_TICKET_SOURCES = (
    "UPDATE tickets SET retailer = NULL, source_payload = NULL WHERE user_id = %s"
)


# --- Journey lifecycle for the delay engine -------------------------------
# These queries touch only journeys-owned tables (journeys, tickets) plus the
# operators reference data. The delays module drives them THROUGH the service,
# never with its own SQL (ARCHITECTURE §3: no cross-module table access).

# Work query, keyset-paginated on (travel_date, scheduled_arrival, id) so a
# sweep pages past journeys it could not decide (source knows nothing yet)
# instead of re-fetching the same stuck head forever. Seasons are excluded:
# tracked, but not assessable (delays cannot price them — see
# delays.service.compute_entitlement).
_AWAITING_ASSESSMENT_SELECT = (
    "SELECT j.id, j.user_id, j.ticket_id, j.operator_id, j.origin_crs, j.destination_crs, "
    "j.travel_date, j.scheduled_departure, j.scheduled_arrival, j.status, "
    "t.price_pence, t.kind AS ticket_kind, "
    "o.min_delay_minutes AS operator_min_delay_minutes "
    "FROM journeys j "
    "JOIN tickets t ON t.id = j.ticket_id "
    "LEFT JOIN operators o ON o.id = j.operator_id "
    "WHERE j.status IN ('pending', 'matched') AND t.kind <> 'season' "
    "AND j.scheduled_arrival <= %s "
)
_AWAITING_ASSESSMENT_ORDER = "ORDER BY j.travel_date, j.scheduled_arrival, j.id LIMIT %s"

_LIST_AWAITING_ASSESSMENT_FIRST = _AWAITING_ASSESSMENT_SELECT + _AWAITING_ASSESSMENT_ORDER
_LIST_AWAITING_ASSESSMENT_AFTER = (
    _AWAITING_ASSESSMENT_SELECT
    + "AND (j.travel_date, j.scheduled_arrival, j.id) > (%s, %s, %s) "
    + _AWAITING_ASSESSMENT_ORDER
)

# Guarded status transitions. The guard doubles as a claim: rowcount 0 means
# another process moved the journey first, and the caller must stop — the same
# 'rowcount is load-bearing' protocol as ON CONFLICT DO NOTHING (core.db).
_MARK_ASSESSED = (
    "UPDATE journeys SET status = 'assessed' WHERE id = %s AND status IN ('pending', 'matched')"
)

# 'unmatched' means "we gave up matching" (0005) — only ever true of a
# 'pending' journey. A 'matched' journey has a service; a source with no data
# for it is the source's gap, not a matching failure.
_MARK_UNMATCHED = "UPDATE journeys SET status = 'unmatched' WHERE id = %s AND status = 'pending'"

# Fill operator_id only when absent: a journey already matched to a service
# carries the authoritative operator; a source's ATOC code never overrides it.
_ASSIGN_OPERATOR = "UPDATE journeys SET operator_id = %s WHERE id = %s AND operator_id IS NULL"


# --- Journey facts for the claims module -----------------------------------

# Batched by design: the claims sweep resolves a whole page of detections in
# one round trip rather than one query per journey. Not user-scoped, unlike
# _GET_FOR_USER — the caller here is a background sweep acting for every user,
# and ownership is already fixed by the detection it started from.
# LEFT JOIN, not JOIN: a journey with no operator must come back so the sweep
# can retire it as unclaimable, rather than silently vanishing from the page.
_CLAIM_CONTEXTS = (
    "SELECT j.id AS journey_id, j.user_id, j.operator_id, j.travel_date, "
    "o.claim_window_days "
    "FROM journeys j LEFT JOIN operators o ON o.id = j.operator_id "
    "WHERE j.id = ANY(%s)"
)

# Batched and un-scoped for the same reasons as _CLAIM_CONTEXTS — the caller
# is the notification worker acting for every user, and the user to notify is
# an answer, not an input.
_NOTIFICATION_CONTEXTS = (
    "SELECT id AS journey_id, user_id, origin_crs, destination_crs, scheduled_departure "
    "FROM journeys WHERE id = ANY(%s)"
)


def list_awaiting_assessment(
    conn: psycopg.Connection,
    cutoff: datetime,
    limit: int,
    after: tuple[date, datetime, UUID] | None = None,
) -> list[AssessableJourney]:
    if after is None:
        return db.fetch_all(
            conn, _LIST_AWAITING_ASSESSMENT_FIRST, (cutoff, limit), row_cls=AssessableJourney
        )
    return db.fetch_all(
        conn, _LIST_AWAITING_ASSESSMENT_AFTER, (cutoff, *after, limit), row_cls=AssessableJourney
    )


def mark_assessed(conn: psycopg.Connection, journey_id: UUID) -> bool:
    """True if this call moved the journey to 'assessed' (the claim was won)."""
    return db.execute(conn, _MARK_ASSESSED, (journey_id,)) == 1


def mark_unmatched(conn: psycopg.Connection, journey_id: UUID) -> bool:
    return db.execute(conn, _MARK_UNMATCHED, (journey_id,)) == 1


def assign_operator(conn: psycopg.Connection, journey_id: UUID, operator_id: UUID) -> None:
    db.execute(conn, _ASSIGN_OPERATOR, (operator_id, journey_id))


def insert_ticket(
    conn: psycopg.Connection,
    user_id: UUID,
    kind: str,
    price_pence: int,
    source: str,
    *,
    retailer: str | None = None,
    source_payload: str | None = None,
) -> UUID:
    return db.fetch_value(
        conn, _INSERT_TICKET, (user_id, kind, price_pence, source, retailer, source_payload)
    )


def insert_journey(
    conn: psycopg.Connection,
    *,
    user_id: UUID,
    ticket_id: UUID,
    origin_crs: str,
    destination_crs: str,
    travel_date: date,
    scheduled_departure: datetime,
    scheduled_arrival: datetime,
) -> JourneyRow:
    row = db.fetch_one(
        conn,
        _INSERT_JOURNEY,
        (
            user_id,
            ticket_id,
            origin_crs,
            destination_crs,
            travel_date,
            scheduled_departure,
            scheduled_arrival,
        ),
        row_cls=JourneyRow,
    )
    if row is None:  # unreachable: INSERT ... RETURNING yields the row or raises
        raise RuntimeError("INSERT ... RETURNING produced no row")
    return row


def get_for_user(conn: psycopg.Connection, journey_id: UUID, user_id: UUID) -> JourneyRow | None:
    return db.fetch_one(conn, _GET_FOR_USER, (journey_id, user_id), row_cls=JourneyRow)


def list_for_user(conn: psycopg.Connection, user_id: UUID, limit: int) -> list[JourneyRow]:
    return db.fetch_all(conn, _LIST_FOR_USER, (user_id, limit), row_cls=JourneyRow)


def leg_exists(
    conn: psycopg.Connection,
    user_id: UUID,
    travel_date: date,
    origin_crs: str,
    destination_crs: str,
    scheduled_departure: datetime,
) -> bool:
    return bool(
        db.fetch_value(
            conn,
            _LEG_EXISTS,
            (user_id, travel_date, origin_crs, destination_crs, scheduled_departure),
        )
    )


def claim_contexts(
    conn: psycopg.Connection, journey_ids: Sequence[UUID]
) -> dict[UUID, ClaimContext]:
    rows = db.fetch_all(conn, _CLAIM_CONTEXTS, (list(journey_ids),), row_cls=ClaimContext)
    return {row.journey_id: row for row in rows}


def notification_contexts(
    conn: psycopg.Connection, journey_ids: Sequence[UUID]
) -> dict[UUID, NotificationContext]:
    rows = db.fetch_all(
        conn, _NOTIFICATION_CONTEXTS, (list(journey_ids),), row_cls=NotificationContext
    )
    return {row.journey_id: row for row in rows}


# --- Inbound ticket emails --------------------------------------------------


def insert_inbound_email(
    conn: psycopg.Connection,
    *,
    user_id: UUID | None,
    message_id: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    spf_pass: bool | None,
    dkim_pass: bool | None,
    forwarding: str | None,
    status: str,
    status_reason: str | None,
    processed_at: datetime | None,
) -> InboundEmailRow | None:
    """The stored row, or None when this message_id was stored before."""
    return db.fetch_one(
        conn,
        _INSERT_INBOUND_EMAIL,
        (
            user_id,
            message_id,
            sender,
            recipient,
            subject,
            body,
            spf_pass,
            dkim_pass,
            forwarding,
            status,
            status_reason,
            processed_at,
        ),
        row_cls=InboundEmailRow,
    )


def known_message_ids(conn: psycopg.Connection, message_ids: Sequence[str]) -> set[str]:
    """Which of `message_ids` are already stored. Empty input asks nothing."""
    if not message_ids:
        return set()
    return set(db.fetch_value(conn, _KNOWN_MESSAGE_IDS, (list(message_ids),)))


def insert_attachment(
    conn: psycopg.Connection,
    inbound_email_id: UUID,
    *,
    filename: str,
    content_type: str,
    content: bytes,
) -> None:
    db.execute(
        conn,
        _INSERT_ATTACHMENT,
        (inbound_email_id, filename, content_type, content, len(content)),
    )


def list_received_emails(
    conn: psycopg.Connection, limit: int, *, excluding: Sequence[UUID] = ()
) -> list[InboundEmailRow]:
    return db.fetch_all(
        conn, _LIST_RECEIVED_EMAILS, (list(excluding), limit), row_cls=InboundEmailRow
    )


def list_inbound_for_user(
    conn: psycopg.Connection, user_id: UUID, limit: int
) -> list[InboundEmailSummary]:
    return db.fetch_all(conn, _LIST_INBOUND_FOR_USER, (user_id, limit), row_cls=InboundEmailSummary)


def bump_attempts(conn: psycopg.Connection, email_id: UUID) -> bool:
    """True if the attempt was counted; False if the row is no longer queued."""
    return db.execute(conn, _BUMP_ATTEMPTS, (email_id,)) == 1


def mark_processed(
    conn: psycopg.Connection,
    email_id: UUID,
    *,
    status: str,
    reason: str | None,
    extraction: dict[str, Any] | None,
) -> bool:
    """True if this call decided the row; False if it was no longer queued."""
    payload = None if extraction is None else Jsonb(extraction)
    return db.execute(conn, _MARK_PROCESSED, (status, reason, payload, email_id)) == 1


def mark_failed_if_exhausted(
    conn: psycopg.Connection, email_id: UUID, *, max_attempts: int, reason: str
) -> bool:
    """True if the row has now used up its attempts and was marked failed."""
    return db.execute(conn, _MARK_FAILED_IF_EXHAUSTED, (reason, email_id, max_attempts)) == 1


def count_received_since(conn: psycopg.Connection, user_id: UUID, since: datetime, cap: int) -> int:
    """How many emails this user has had accepted since `since`, counted no
    further than `cap` — the caller only needs to know whether the cap is
    reached."""
    return db.fetch_value(conn, _COUNT_RECEIVED_SINCE, (user_id, since, cap))


def purge_old_bodies(conn: psycopg.Connection, before: datetime, limit: int) -> int:
    """How many bodies this call blanked."""
    return db.execute(conn, _PURGE_OLD_BODIES, (before, limit))


def purge_attachments_of_purged_bodies(conn: psycopg.Connection, limit: int) -> int:
    """Delete attachments whose email body has already been blanked. How
    many."""
    return db.execute(conn, _PURGE_PURGED_ATTACHMENTS, (limit,))


def retire_stale_received(
    conn: psycopg.Connection, before: datetime, limit: int, *, reason: str
) -> int:
    """Retire and blank emails that were never read and are now past the
    window. How many."""
    return db.execute(conn, _RETIRE_STALE_RECEIVED, (reason, before, limit))


# --- Erasure ----------------------------------------------------------------


def delete_inbound_emails(conn: psycopg.Connection, user_id: UUID) -> int:
    return db.execute(conn, _DELETE_INBOUND_EMAILS, (user_id,))


def strip_ticket_sources(conn: psycopg.Connection, user_id: UUID) -> int:
    return db.execute(conn, _STRIP_TICKET_SOURCES, (user_id,))
