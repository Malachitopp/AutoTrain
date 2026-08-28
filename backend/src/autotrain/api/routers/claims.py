"""Claim endpoints.

Thin by rule (ARCHITECTURE §3): parse the request, call the claims service,
shape the response. Claims are CREATED by the scheduler's sweep and MOVED by
the state machine's owners (adapters, status ingestion) — so the one write
surface here, POST /{claim_id}/file, obeys the same rule: the deep-link
adapter owns the move (draft -> needs_user); the router only asks for it on
the user's behalf.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from autotrain.api.deps import ConnDep, UserIdDep
from autotrain.api.schemas import (
    ClaimEventOut,
    ClaimFilingOut,
    ClaimOut,
    ClaimPage,
    ClaimSummaryOut,
)
from autotrain.modules.claims import service

router = APIRouter(prefix="/claims", tags=["claims"])


@router.get("")
def list_claims(
    conn: ConnDep,
    user_id: UserIdDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ClaimPage:
    rows = service.list_claims(conn, user_id, limit=limit)
    items = [ClaimOut.model_validate(row) for row in rows]
    return ClaimPage(items=items, count=len(items), limit=limit)


@router.get("/summary")
def total_claims(user_id: UserIdDep, conn: ConnDep) -> ClaimSummaryOut:

    return ClaimSummaryOut.model_validate(service.claims_total(conn, user_id))


@router.get("/{claim_id}")
def get_claim(claim_id: UUID, conn: ConnDep, user_id: UserIdDep) -> ClaimOut:
    row = service.get_claim(conn, claim_id, user_id)
    if row is None:
        # Absent and not-yours answer identically: existence never leaks.
        raise HTTPException(status_code=404, detail="claim not found")
    return ClaimOut.model_validate(row)


@router.post("/{claim_id}/file")
def file_claim(claim_id: UUID, conn: ConnDep, user_id: UserIdDep) -> ClaimFilingOut:
    try:
        filing = service.file_claim(conn, claim_id, user_id)
    except (service.NotFilable, service.FilingUnavailable) as exc:
        # Both are "the request was fine; the claim's state says no" — already
        # filed or resolved, or the operator has no working link yet.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if filing is None:
        # Absent and not-yours answer identically: existence never leaks.
        raise HTTPException(status_code=404, detail="claim not found")
    return ClaimFilingOut.model_validate(filing)


@router.get("/{claim_id}/events")
def claim_events(claim_id: UUID, conn: ConnDep, user_id: UserIdDep) -> list[ClaimEventOut]:
    # claim_history is deliberately not ownership-scoped (its other callers
    # are internal), so ownership is established here first — without this
    # check any authenticated user could read any claim's audit trail by
    # guessing ids.
    if service.get_claim(conn, claim_id, user_id) is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return [ClaimEventOut.model_validate(row) for row in service.claim_history(conn, claim_id)]
