"""SQL for the delays module — same rules as the journeys repository.

Constants are deliberately unannotated so pyright keeps their LiteralString
type; values travel as `%s` parameters, never in the text. Every function
takes the connection first and never commits — the caller owns the
transaction (ARCHITECTURE §3).

Scope note: only delays-owned data (delay_detections) and the operators
reference tables appear here. The journeys work query and status transitions
belong to the journeys module and are reached through journeys.service.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import psycopg

from autotrain.core import db
from autotrain.modules.delays.models import (
    Band,
    DelayDecision,
    OperatorRef,
    PendingNotification,
    UnclaimedDetection,
)

# Guarantee 1 of migration 0006: at most one detection per journey. Rowcount 0
# means another process decided first — the caller stops, never overwrites.
_INSERT_DETECTION = (
    "INSERT INTO delay_detections "
    "(journey_id, actual_arrival, delay_minutes, source, band_percent, entitlement_pence) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (journey_id) DO NOTHING"
)

# No is_active filter, deliberately: entitlement reflects the scheme at time
# of travel (0006), and a franchise change between travel and assessment must
# not zero a legitimate claim. is_active gates future *filing* adapters, not
# pricing.
_OPERATOR_BY_ATOC = "SELECT id, min_delay_minutes FROM operators WHERE atoc_code = %s"

_BANDS_FOR_OPERATOR = (
    "SELECT min_minutes, max_minutes, percent, of_return_fare "
    "FROM delay_repay_bands WHERE operator_id = %s ORDER BY min_minutes"
)

# --- The claims module's work queue (0010) --------------------------------
# Single-table by design: claims cannot join delay_detections and neither can
# delays join claims, so "does a claim exist yet" is answered by
# claims_processed_at rather than an anti-join (0010's design note).

# No keyset cursor, unlike the journeys work query: every row this returns is
# stamped processed by the end of the sweep's transaction, so the work set
# strictly shrinks and the head always advances. A row that errors is the one
# exception, which is why the sweep stops when a page yields no progress.
_LIST_UNCLAIMED = (
    "SELECT id, journey_id, entitlement_pence, observed_at FROM delay_detections "
    "WHERE claims_processed_at IS NULL AND entitlement_pence > 0 "
    "ORDER BY observed_at, id LIMIT %s"
)

# Guarded so the stamp is written once: rowcount 0 means another claims sweep
# already finished deciding about this detection.
_MARK_CLAIMS_PROCESSED = (
    "UPDATE delay_detections SET claims_processed_at = now() "
    "WHERE id = %s AND claims_processed_at IS NULL"
)

# The notification worker's queue, exactly as 0006 prescribed it ("worker:
# WHERE notified_at IS NULL FOR UPDATE SKIP LOCKED") and as
# delay_detections_pending_idx serves it. FOR UPDATE locks the returned rows
# until the caller's transaction settles, and SKIP LOCKED makes a second
# worker glide past them instead of blocking — two workers can drain the same
# queue without ever picking up the same detection.
_LIST_UNNOTIFIED = (
    "SELECT id, journey_id, delay_minutes, band_percent, entitlement_pence, observed_at "
    "FROM delay_detections "
    "WHERE notified_at IS NULL AND entitlement_pence > 0 "
    "ORDER BY observed_at, id LIMIT %s "
    "FOR UPDATE SKIP LOCKED"
)

# Guarded like the claims stamp: the exactly-once guarantee (0006 guarantee 3)
# is the row lock above plus this stamp committing with the send.
_MARK_NOTIFIED = (
    "UPDATE delay_detections SET notified_at = now() WHERE id = %s AND notified_at IS NULL"
)

# Read path for the API. Deliberately NOT ownership-scoped: journeys owns the
# user->journey relationship, so the caller establishes ownership through
# journeys.service first — a JOIN to journeys here would cross the module
# boundary (ARCHITECTURE §3).
_DECISION_FOR_JOURNEY = (
    "SELECT actual_arrival, delay_minutes, source, band_percent, entitlement_pence, observed_at "
    "FROM delay_detections WHERE journey_id = %s"
)


def insert_detection(
    conn: psycopg.Connection,
    *,
    journey_id: UUID,
    actual_arrival: datetime,
    delay_minutes: int,
    source: str,
    band_percent: int | None,
    entitlement_pence: int,
) -> bool:
    """True if this call wrote the detection; False if one already existed."""
    return (
        db.execute(
            conn,
            _INSERT_DETECTION,
            (journey_id, actual_arrival, delay_minutes, source, band_percent, entitlement_pence),
        )
        == 1
    )


def operator_by_atoc(conn: psycopg.Connection, atoc_code: str) -> OperatorRef | None:
    return db.fetch_one(conn, _OPERATOR_BY_ATOC, (atoc_code,), row_cls=OperatorRef)


def bands_for_operator(conn: psycopg.Connection, operator_id: UUID) -> list[Band]:
    return db.fetch_all(conn, _BANDS_FOR_OPERATOR, (operator_id,), row_cls=Band)


def list_unclaimed(conn: psycopg.Connection, limit: int) -> list[UnclaimedDetection]:
    return db.fetch_all(conn, _LIST_UNCLAIMED, (limit,), row_cls=UnclaimedDetection)


def mark_claims_processed(conn: psycopg.Connection, detection_id: UUID) -> bool:
    return db.execute(conn, _MARK_CLAIMS_PROCESSED, (detection_id,)) == 1


def list_unnotified(conn: psycopg.Connection, limit: int) -> list[PendingNotification]:
    return db.fetch_all(conn, _LIST_UNNOTIFIED, (limit,), row_cls=PendingNotification)


def mark_notified(conn: psycopg.Connection, detection_id: UUID) -> bool:
    return db.execute(conn, _MARK_NOTIFIED, (detection_id,)) == 1


def decision_for_journey(conn: psycopg.Connection, journey_id: UUID) -> DelayDecision | None:
    return db.fetch_one(conn, _DECISION_FOR_JOURNEY, (journey_id,), row_cls=DelayDecision)
