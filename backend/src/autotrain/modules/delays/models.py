"""Row shapes for the delays module's queries.

Frozen dataclasses matched by column name (`class_row`), exactly as in the
journeys module. The sweep's work-row shape (AssessableJourney) lives in the
journeys module — those are its tables — and reaches delays through
journeys.service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Band:
    """One row of an operator's Delay Repay scale (delay_repay_bands, 0003).
    The band is [min_minutes, max_minutes); max NULL = unbounded top band."""

    min_minutes: int
    max_minutes: int | None
    percent: int
    of_return_fare: bool


@dataclass(frozen=True)
class OperatorRef:
    """The two operator facts the sweep needs when resolving by ATOC code."""

    id: UUID
    min_delay_minutes: int


@dataclass(frozen=True)
class ArrivalReport:
    """What an arrivals source knows about one journey's real outcome."""

    actual_arrival: datetime
    # 'darwin' or 'hsp' — recorded on the detection (0006 CHECK constraint).
    source: str
    # The operating TOC as reported by the source, when known. Used to resolve
    # operator_id for manually-entered journeys that never named an operator.
    atoc_code: str | None = None


@dataclass(frozen=True)
class UnclaimedDetection:
    """One entitled detection the claims module has not decided about yet
    (claims_processed_at IS NULL, 0010). Deliberately narrow: it carries only
    delay_detections columns, because everything else a claim needs —
    user, operator, travel date — belongs to journeys and reaches claims
    through journeys.service."""

    id: UUID
    journey_id: UUID
    entitlement_pence: int
    observed_at: datetime
