"""Row shapes for the journeys module's tables.

Frozen dataclasses rather than Pydantic models: rows never cross the module
boundary unvalidated by Postgres, so re-validating them in Python would only
restate the schema. Field names must match column names exactly — `core.db`
maps rows by name (`class_row`), so a SELECT column with no matching field
fails loudly instead of shape-shifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID


@dataclass(frozen=True)
class TicketRow:
    """One row of `tickets` (migration 0005). price_pence IS PENCE."""

    id: UUID
    user_id: UUID
    kind: str
    price_pence: int
    retailer: str | None
    source: str
    source_payload: str | None
    purchased_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AssessableJourney:
    """One journey awaiting delay assessment: past its scheduled arrival and
    still 'pending'/'matched'. The shape of the delay sweep's work query —
    ticket fields ride along so entitlement can be priced without a second
    query. Owned by journeys (its tables), consumed by the delays module."""

    id: UUID
    user_id: UUID
    ticket_id: UUID
    operator_id: UUID | None
    origin_crs: str
    destination_crs: str
    travel_date: date
    scheduled_departure: datetime
    scheduled_arrival: datetime
    status: str
    price_pence: int
    ticket_kind: str
    # NULL when the journey has no operator yet (LEFT JOIN) — the sweep may
    # still resolve one from the arrival report's ATOC code.
    operator_min_delay_minutes: int | None


@dataclass(frozen=True)
class ClaimContext:
    """The journey facts a Delay Repay claim is built from, for one journey.

    Owned by journeys (its tables) and consumed by the claims module, exactly
    as AssessableJourney is owned here and consumed by delays. claim_window_days
    rides along from the operators LEFT JOIN so the filing deadline can be
    frozen without a second query; both it and operator_id are NULL together,
    which is precisely the case that cannot become a claim.
    """

    journey_id: UUID
    user_id: UUID
    operator_id: UUID | None
    travel_date: date
    claim_window_days: int | None


@dataclass(frozen=True)
class NotificationContext:
    """The journey facts a user-facing message is built from, for one journey:
    who to tell, and which train the news is about ("your 08:14 to EUS").

    Owned by journeys and consumed by the notification worker, exactly as
    ClaimContext is consumed by claims. CRS codes, not station names — a
    stations reference table is future work, and the code is honest until
    then."""

    journey_id: UUID
    user_id: UUID
    origin_crs: str
    destination_crs: str
    scheduled_departure: datetime


@dataclass(frozen=True)
class JourneyRow:
    """One row of `journeys` (migration 0005): one dated leg being monitored."""

    id: UUID
    user_id: UUID
    ticket_id: UUID
    operator_id: UUID | None
    origin_crs: str
    destination_crs: str
    travel_date: date
    scheduled_departure: datetime
    scheduled_arrival: datetime
    darwin_rid: str | None
    darwin_uid: str | None
    status: str
    created_at: datetime
    updated_at: datetime
