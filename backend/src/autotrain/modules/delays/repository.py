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
from autotrain.modules.delays.models import Band, OperatorRef

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
