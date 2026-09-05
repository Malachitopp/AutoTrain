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


@dataclass(frozen=True)
class OperatorFiling:
    """How claims are filed with one operator — the slice of the `operators`
    reference data (0003) that filing reads, mirroring delays' OperatorRef.

    is_active gates FILING, not pricing (the delays repository documents the
    other half of that rule): a franchise change kills the claims portal, but
    never a past entitlement.

    id and name ride along for the API, which looks these rows up by journey to
    say which operator a journey is on and whether we can file with it."""

    id: UUID
    name: str
    adapter: str
    claim_url: str | None
    is_active: bool

    @property
    def is_supported(self) -> bool:
        """Whether AutoTrain can file with this operator: a live adapter, and
        the operator still taking claims. The Python twin of the SQL in the
        repository's _SUPPORTED_OPERATORS — change both together."""
        return self.adapter != "none" and self.is_active


@dataclass(frozen=True)
class SupportedOperator:
    """An operator AutoTrain can file with (OperatorFiling.is_supported), as
    the public GET /operators list shows it: the code, the name, and the delay
    at which its Delay Repay scheme starts paying. Not "automatic": v1 filing
    opens the operator's own form (adapters.py)."""

    atoc_code: str
    name: str
    min_delay_minutes: int
