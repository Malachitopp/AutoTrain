"""SQL for the journeys module — the only file in the module that contains any.

Constants are deliberately unannotated: pyright then keeps their LiteralString
type, which is what lets `core.db` reject any statement assembled from runtime
values (ARCHITECTURE §3). Values travel as `%s` parameters, never in the text.

Every function takes the connection first and never commits — the caller owns
the transaction, which is how ticket + journey creation stays atomic without
either insert knowing about the other.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

import psycopg

from autotrain.core import db
from autotrain.modules.journeys.models import AssessableJourney, JourneyRow

# The full journeys column list is repeated verbatim in each statement below:
# core.db maps rows by name and treats an unexpected column as an error, so
# SELECT * never appears — and building the list by concatenation would trip
# both S608 and the LiteralString guarantee these constants exist to keep.

_INSERT_TICKET = (
    "INSERT INTO tickets (user_id, kind, price_pence, source) VALUES (%s, %s, %s, %s) RETURNING id"
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
    conn: psycopg.Connection, user_id: UUID, kind: str, price_pence: int, source: str
) -> UUID:
    return db.fetch_value(conn, _INSERT_TICKET, (user_id, kind, price_pence, source))


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
