"""The journeys module's public API.

Everything the rest of the system may do with journeys goes through this file
(ARCHITECTURE §3): the api layer and other modules import it and nothing else
in the module. psycopg's exception types never escape it either — database
failures surface as the domain exceptions below, so callers stay free of
driver knowledge.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

# Private alias on purpose: a plain `import repository` would bind the module
# onto this namespace, letting `from ...journeys.service import repository`
# hand callers the repository past every import-linter contract (the graph
# records only an import of the service). The underscore makes that an
# ImportError, so the public surface is exactly the functions below.
from autotrain.modules.journeys import repository as _repository

# AssessableJourney is re-exported deliberately: it is the shape this service
# hands the delay engine, and importing it from here keeps callers off
# journeys.models (the journeys-privacy contract).
from autotrain.modules.journeys.models import AssessableJourney, JourneyRow

# The two constraints that mean "this journey is already tracked":
# journeys_user_leg_departure_key (0009) is the cross-request guard — every
# manual add mints a fresh ticket, so only a user-scoped key can catch a
# double-add; journeys_ticket_leg_key (0005) guards re-adds of the SAME
# ticket, for future callers that reuse one (a return's second leg, barcode
# re-parse).
_DUPLICATE_CONSTRAINTS = frozenset({"journeys_user_leg_departure_key", "journeys_ticket_leg_key"})


class JourneysError(Exception):
    """Base for journeys domain failures."""


class DuplicateJourney(JourneysError):
    """The user already tracks this departure on this travel date."""


class UnknownUser(JourneysError):
    """The user id references no user row — possible while auth is a stub."""


class InvalidJourney(JourneysError):
    """The values violate a database-enforced invariant (price out of range,
    unordered times). The API schemas reject these at the edge; this exists
    for direct service callers, so driver exceptions still never escape."""


def add_manual_journey(
    conn: psycopg.Connection,
    user_id: UUID,
    *,
    kind: str,
    price_pence: int,
    origin_crs: str,
    destination_crs: str,
    travel_date: date,
    scheduled_departure: datetime,
    scheduled_arrival: datetime,
) -> JourneyRow:
    """Create the ticket and its journey as one unit of work.

    The caller owns the transaction, so the two inserts commit or roll back
    together — a journey can never exist without its ticket, and a failed
    journey insert leaves no orphaned ticket behind.

    Duplicate policy: the leg_exists pre-check answers the common case (the
    same trip re-submitted sequentially) before any insert, but the guarantee
    itself is journeys_user_leg_departure_key in the database — two concurrent
    adds both pass the pre-check, and the race loser's INSERT then hits the
    index and is translated to DuplicateJourney below. A duplicate can get a
    409 two ways; it can never get stored twice.
    """
    if _repository.leg_exists(
        conn, user_id, travel_date, origin_crs, destination_crs, scheduled_departure
    ):
        raise DuplicateJourney(
            f"{origin_crs}->{destination_crs} on {travel_date} at {scheduled_departure} "
            "is already tracked"
        )
    try:
        ticket_id = _repository.insert_ticket(conn, user_id, kind, price_pence, "manual")
        return _repository.insert_journey(
            conn,
            user_id=user_id,
            ticket_id=ticket_id,
            origin_crs=origin_crs,
            destination_crs=destination_crs,
            travel_date=travel_date,
            scheduled_departure=scheduled_departure,
            scheduled_arrival=scheduled_arrival,
        )
    except pg_errors.ForeignKeyViolation as exc:
        # Only the users FK can fail here — no operator id is supplied on a
        # manual add — so this is always "that user does not exist".
        raise UnknownUser(str(user_id)) from exc
    except pg_errors.UniqueViolation as exc:
        if exc.diag.constraint_name in _DUPLICATE_CONSTRAINTS:
            raise DuplicateJourney(
                f"{origin_crs}->{destination_crs} on {travel_date} at {scheduled_departure} "
                "is already tracked"
            ) from exc
        raise
    except (pg_errors.CheckViolation, pg_errors.DataError) as exc:
        # CHECKs (times ordered, CRS shape, price >= 0) and range errors
        # (price_pence past int4). API callers never reach this — the schemas
        # mirror every one of these constraints — but a direct caller must
        # still get a domain error, not a driver class.
        raise InvalidJourney(str(exc).strip()) from exc


def get_journey(conn: psycopg.Connection, journey_id: UUID, user_id: UUID) -> JourneyRow | None:
    """The journey, or None when absent — including "exists but is not yours":
    ownership is part of the lookup, so existence never leaks across users."""
    return _repository.get_for_user(conn, journey_id, user_id)


def list_journeys(conn: psycopg.Connection, user_id: UUID, limit: int = 50) -> list[JourneyRow]:
    """The user's journeys, newest travel date first."""
    return _repository.list_for_user(conn, user_id, limit)


# --- Journey lifecycle for the delay engine -------------------------------
# journeys owns its tables and its status machine; the delays module drives
# these transitions through here rather than with its own SQL
# (ARCHITECTURE §3: no cross-module table access).


def list_awaiting_assessment(
    conn: psycopg.Connection,
    cutoff: datetime,
    limit: int,
    after: tuple[date, datetime, UUID] | None = None,
) -> list[AssessableJourney]:
    """Journeys past their scheduled arrival still needing a delay decision,
    oldest first. `after` is the keyset cursor — pass the last row's
    (travel_date, scheduled_departure-ordering key, id) to page past journeys
    the caller examined but could not decide."""
    return _repository.list_awaiting_assessment(conn, cutoff, limit, after)


def mark_assessed(conn: psycopg.Connection, journey_id: UUID) -> bool:
    """Claim the journey for assessment: True moves 'pending'/'matched' →
    'assessed'; False means another process moved it first and the caller
    must stop (the rowcount-is-load-bearing protocol, core.db)."""
    return _repository.mark_assessed(conn, journey_id)


def mark_unmatched(conn: psycopg.Connection, journey_id: UUID) -> bool:
    """Retire a 'pending' journey we gave up on. Guarded to 'pending' only —
    a 'matched' journey has a service, so lacking data is never a matching
    failure (0005's status semantics)."""
    return _repository.mark_unmatched(conn, journey_id)


def assign_operator(conn: psycopg.Connection, journey_id: UUID, operator_id: UUID) -> None:
    """Attach an operator to a journey that has none. Never overrides an
    existing match."""
    _repository.assign_operator(conn, journey_id, operator_id)
