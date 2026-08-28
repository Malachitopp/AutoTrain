"""SQL for the claims module — same rules as the journeys and delays repositories.

Constants are deliberately unannotated so pyright keeps their LiteralString
type; values travel as `%s` parameters, never in the text. Every function takes
the connection first and never commits — the caller owns the transaction, which
is what lets a status change and its audit event land atomically.

Scope note: only claims-owned tables (claims, claim_events) appear here. The
detection being claimed and the journey it belongs to are read through
delays.service and journeys.service (ARCHITECTURE §3).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from autotrain.core import db
from autotrain.modules.claims.models import ClaimEventRow, ClaimRow, ClaimTotal

# The full claims column list is repeated verbatim in each statement below, for
# the same two reasons as in the journeys repository: core.db maps rows by name
# and treats an unexpected column as an error, so SELECT * never appears — and
# hoisting the list into a shared constant would trip both S608 and the
# LiteralString guarantee these constants exist to keep.

# Guarantee 2 of migration 0006: at most one claim per journey. No row comes
# back on conflict, which is the signal that another sweep created it first.
#
# ON CONFLICT names journey_id even though detection_id is UNIQUE too: one
# journey has at most one detection (0006 guarantee 1, and a superseded
# detection is UPDATEd rather than re-inserted), so the two constraints can
# only ever fire together and the journey is the one to name.
#
# status is left to its DEFAULT 'draft' — every claim starts there and reaches
# any other state through the audited state machine in service.py.
_INSERT_CLAIM = (
    "INSERT INTO claims (journey_id, detection_id, operator_id, user_id, amount_pence, file_by) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (journey_id) DO NOTHING "
    "RETURNING id, journey_id, detection_id, operator_id, user_id, amount_pence, status, "
    "submission_token, file_by, submitted_at, resolved_at, operator_reference, "
    "created_at, updated_at"
)

# Append-only: claim_events is never updated or deleted (0006).
#
# created_at is written explicitly, overriding the column's DEFAULT now(),
# because now() is TRANSACTION time: two events written in one transaction —
# a claim created and immediately moved on, an expiry sweep row — would share
# a timestamp exactly, and the history would come back in index order, which is
# random uuid order. clock_timestamp() reads the wall clock per statement, so
# the audit trail orders by when things actually happened.
_INSERT_EVENT = (
    "INSERT INTO claim_events (claim_id, from_status, to_status, detail, created_at) "
    "VALUES (%s, %s, %s, %s, clock_timestamp())"
)

# Guarded on the status we believe the claim is in, so the read-then-write in
# service.transition is race-safe: rowcount 0 means another writer moved the
# claim between the read and this update, and the caller must stop (the
# 'rowcount is load-bearing' protocol, core.db).
#
# The two timestamps are derived from the destination state rather than passed
# in, so they can never disagree with `status`, and they use the database clock
# like every other timestamp in the schema. resolved_at deliberately excludes
# 'approved': the operator has agreed, but the money has not arrived, so the
# claim is not finished.
_TRANSITION = (
    "UPDATE claims SET status = %(to_status)s, "
    "submitted_at = CASE WHEN %(to_status)s = 'submitted' THEN now() ELSE submitted_at END, "
    "resolved_at = CASE WHEN %(to_status)s IN ('paid', 'rejected', 'expired') "
    "THEN now() ELSE resolved_at END "
    "WHERE id = %(claim_id)s AND status = %(from_status)s"
)

_GET_BY_ID = (
    "SELECT id, journey_id, detection_id, operator_id, user_id, amount_pence, status, "
    "submission_token, file_by, submitted_at, resolved_at, operator_reference, "
    "created_at, updated_at "
    "FROM claims WHERE id = %s"
)

# Ownership lives in the WHERE clause, as in journeys: a claim belonging to
# someone else is indistinguishable from one that does not exist.
_GET_FOR_USER = (
    "SELECT id, journey_id, detection_id, operator_id, user_id, amount_pence, status, "
    "submission_token, file_by, submitted_at, resolved_at, operator_reference, "
    "created_at, updated_at "
    "FROM claims WHERE id = %s AND user_id = %s"
)

# Exactly the shape claims_user_idx (0006) serves.
_LIST_FOR_USER = (
    "SELECT id, journey_id, detection_id, operator_id, user_id, amount_pence, status, "
    "submission_token, file_by, submitted_at, resolved_at, operator_reference, "
    "created_at, updated_at "
    "FROM claims WHERE user_id = %s ORDER BY created_at DESC LIMIT %s"
)

# The deadline sweep's work query, matching claims_workable_idx (0006).
# 'needs_user' is deliberately absent: the user was handed a deep link and may
# have filed it themselves, so we do not know the claim is dead and will not
# say so in their history.
_LIST_OVERDUE = (
    "SELECT id, status FROM claims WHERE file_by < %s AND status IN ('draft', 'ready') "
    "ORDER BY file_by, id LIMIT %s"
)

_EVENTS_FOR_CLAIM = (
    "SELECT id, claim_id, from_status, to_status, detail, created_at "
    "FROM claim_events WHERE claim_id = %s ORDER BY created_at, id"
)

_TOTALS_FOR_USER = (
    "SELECT COALESCE(SUM(amount_pence) FILTER (WHERE status = 'paid'), 0) AS recovered_pence,"
    "COALESCE(SUM(amount_pence) FILTER "
    "(WHERE status IN ('draft', 'ready', 'needs_user',"
    "'submitted', 'approved')), 0) AS pending_pence "
    "FROM claims "
    "WHERE user_id = %s"
)


def insert_claim(
    conn: psycopg.Connection,
    *,
    journey_id: UUID,
    detection_id: UUID,
    operator_id: UUID,
    user_id: UUID,
    amount_pence: int,
    file_by: date,
) -> ClaimRow | None:
    """The new claim, or None if one already existed for this journey."""
    return db.fetch_one(
        conn,
        _INSERT_CLAIM,
        (journey_id, detection_id, operator_id, user_id, amount_pence, file_by),
        row_cls=ClaimRow,
    )


def insert_event(
    conn: psycopg.Connection,
    *,
    claim_id: UUID,
    from_status: str | None,
    to_status: str,
    detail: str | None,
) -> None:
    db.execute(conn, _INSERT_EVENT, (claim_id, from_status, to_status, detail))


def transition(
    conn: psycopg.Connection, *, claim_id: UUID, from_status: str, to_status: str
) -> bool:
    """True if this call moved the claim; False if it was no longer in
    `from_status` (another writer got there first)."""
    return (
        db.execute(
            conn,
            _TRANSITION,
            {"claim_id": claim_id, "from_status": from_status, "to_status": to_status},
        )
        == 1
    )


def get_by_id(conn: psycopg.Connection, claim_id: UUID) -> ClaimRow | None:
    return db.fetch_one(conn, _GET_BY_ID, (claim_id,), row_cls=ClaimRow)


def get_for_user(conn: psycopg.Connection, claim_id: UUID, user_id: UUID) -> ClaimRow | None:
    return db.fetch_one(conn, _GET_FOR_USER, (claim_id, user_id), row_cls=ClaimRow)


def list_for_user(conn: psycopg.Connection, user_id: UUID, limit: int) -> list[ClaimRow]:
    return db.fetch_all(conn, _LIST_FOR_USER, (user_id, limit), row_cls=ClaimRow)


def list_overdue(conn: psycopg.Connection, today: date, limit: int) -> list[tuple[UUID, str]]:
    """(claim_id, current_status) for claims past their filing deadline that
    are still awaiting filing. Returned as tuples rather than a row class: the
    expiry sweep needs the id to transition and the status to guard on, and
    nothing else."""
    with conn.cursor() as cur:
        cur.execute(_LIST_OVERDUE, (today, limit))
        return [(row[0], row[1]) for row in cur.fetchall()]


def events_for_claim(conn: psycopg.Connection, claim_id: UUID) -> list[ClaimEventRow]:
    return db.fetch_all(conn, _EVENTS_FOR_CLAIM, (claim_id,), row_cls=ClaimEventRow)


def totals_for_user(conn: psycopg.Connection, user_id: UUID) -> ClaimTotal:
    row = db.fetch_one(conn, _TOTALS_FOR_USER, (user_id,), row_cls=ClaimTotal)
    if row is None:
        raise RuntimeError("totals aggregate produced no row")
    return row
