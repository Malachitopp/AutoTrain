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
