"""Row shapes for the claims module's tables.

Frozen dataclasses matched by column name (`class_row`), exactly as in the
journeys and delays modules. The two shapes a claim is BUILT from live in the
modules that own them — journeys.service.ClaimContext and
delays.service.UnclaimedDetection — and reach claims through those services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID


@dataclass(frozen=True)
class ClaimRow:
    """One row of `claims` (migration 0006): one Delay Repay claim, at most one
    per journey. amount_pence IS PENCE and is frozen from the detection's
    entitlement — the operator may later approve a different figure, which is
    recorded as a transition, never by editing this."""

    id: UUID
    journey_id: UUID
    detection_id: UUID
    operator_id: UUID
    user_id: UUID
    amount_pence: int
    status: str
    submission_token: UUID
    file_by: date
    submitted_at: datetime | None
    resolved_at: datetime | None
    operator_reference: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ClaimEventRow:
    """One row of `claim_events` (migration 0006): an append-only record of a
    single status transition. from_status NULL is the creation event."""

    id: UUID
    claim_id: UUID
    from_status: str | None
    to_status: str
    detail: str | None
    created_at: datetime


@dataclass(frozen=True)
class ClaimTotal:
    """Recovered pence is the total that the user has received, and pending
    is the amount pending to be digested"""

    recovered_pence: int
    pending_pence: int
