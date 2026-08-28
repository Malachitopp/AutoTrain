"""Journey endpoints.

Thin by rule (ARCHITECTURE §3): parse the request, call the journeys service,
shape the response. Domain exceptions map to status codes here; SQL and psycopg
stay behind the service.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from autotrain.api.deps import ConnDep, UserIdDep
from autotrain.api.schemas import DecisionOut, JourneyCreate, JourneyOut, JourneyPage
from autotrain.modules.delays import service as delays
from autotrain.modules.journeys import service

router = APIRouter(prefix="/journeys", tags=["journeys"])


@router.post("", status_code=201)
def create_journey(payload: JourneyCreate, conn: ConnDep, user_id: UserIdDep) -> JourneyOut:
    try:
        row = service.add_manual_journey(
            conn,
            user_id,
            kind=payload.kind,
            price_pence=payload.price_pence,
            origin_crs=payload.origin_crs,
            destination_crs=payload.destination_crs,
            travel_date=payload.travel_date,
            scheduled_departure=payload.scheduled_departure,
            scheduled_arrival=payload.scheduled_arrival,
        )
    except service.UnknownUser as exc:
        # The stub auth header can name a nonexistent user: a bad reference
        # from the client, not a server fault — 404, never a 500.
        raise HTTPException(status_code=404, detail="unknown user") from exc
    except service.DuplicateJourney as exc:
        raise HTTPException(status_code=409, detail="journey already added") from exc
    except service.InvalidJourney as exc:
        # Backstop only: JourneyCreate mirrors every constraint that can raise
        # this, so a request should fail as a schema 422 before any SQL runs.
        raise HTTPException(status_code=422, detail="invalid journey values") from exc
    return JourneyOut.model_validate(row)


@router.get("")
def list_journeys(
    conn: ConnDep,
    user_id: UserIdDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JourneyPage:
    rows = service.list_journeys(conn, user_id, limit=limit)
    items = [JourneyOut.model_validate(row) for row in rows]
    return JourneyPage(items=items, count=len(items), limit=limit)


@router.get("/{journey_id}")
def get_journey(journey_id: UUID, conn: ConnDep, user_id: UserIdDep) -> JourneyOut:
    row = service.get_journey(conn, journey_id, user_id)
    if row is None:
        # Absent and not-yours answer identically: existence never leaks.
        raise HTTPException(status_code=404, detail="journey not found")
    return JourneyOut.model_validate(row)


@router.get("/{journey_id}/decision")
def get_journey_decision(journey_id: UUID, conn: ConnDep, user_id: UserIdDep) -> DecisionOut:
    """The journey's frozen delay decision — was it late, and what is it worth.

    Ownership is journeys' knowledge and the decision is delays' — hence two
    service calls: the first is the ownership gate (delays.decision_for_journey
    is deliberately unscoped), the second the read. Both 404s look identical
    to a stranger; the detail strings differ only for the journey's owner.
    """
    if service.get_journey(conn, journey_id, user_id) is None:
        raise HTTPException(status_code=404, detail="journey not found")
    decision = delays.decision_for_journey(conn, journey_id)
    if decision is None:
        # Owned, but not yet decided: still awaiting the sweep (or the source).
        raise HTTPException(status_code=404, detail="no delay decision yet")
    return DecisionOut.model_validate(decision)
