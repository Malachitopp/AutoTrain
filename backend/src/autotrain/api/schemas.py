"""Wire shapes for the journeys endpoints.

Validation happens here, before any SQL runs: a request that would violate a
0005 constraint fails as a 422 naming the offending field, not as a database
error. The database CHECKs stay authoritative — these models just move the
rejection to the edge.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)


class JourneyCreate(BaseModel):
    """A manually entered journey (one ticket, one leg)."""

    origin_crs: str = Field(pattern=r"^[A-Z]{3}$")
    destination_crs: str = Field(pattern=r"^[A-Z]{3}$")
    # The UK timetable day, supplied explicitly rather than derived from the
    # departure instant — deriving it breaks on BST edges (see migration 0005).
    travel_date: date
    # AwareDatetime: a naive datetime is ambiguous around BST, so it is
    # rejected at the edge rather than guessed at (ARCHITECTURE §9, "Time").
    scheduled_departure: AwareDatetime
    scheduled_arrival: AwareDatetime
    # gt: stricter than the DB's >= 0 — a manually added free ticket is a
    # data-entry error. le: the int4 column ceiling; without it a Python int
    # (unbounded) sails past validation and dies in the INSERT as a driver
    # range error — a 500 — instead of a 422 naming the field.
    price_pence: int = Field(gt=0, le=2_147_483_647)
    kind: Literal["single", "return", "season"] = "single"

    @model_validator(mode="after")
    def _arrival_follows_departure(self) -> JourneyCreate:
        if self.scheduled_arrival <= self.scheduled_departure:
            raise ValueError("scheduled_arrival must be after scheduled_departure")
        return self


class JourneyOut(BaseModel):
    """A journey as the API reports it. Built straight from a JourneyRow."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    origin_crs: str
    destination_crs: str
    travel_date: date
    scheduled_departure: AwareDatetime
    scheduled_arrival: AwareDatetime
    status: str
    created_at: AwareDatetime

    @field_serializer("scheduled_departure", "scheduled_arrival", "created_at")
    def _in_utc(self, value: datetime) -> datetime:
        # timestamptz comes back in the session timezone; the API speaks UTC
        # only — Europe/London exists at render time, in the clients.
        return value.astimezone(UTC)


class JourneyPage(BaseModel):
    """One page of a user's journeys, newest travel date first."""

    items: list[JourneyOut]
    count: int
    limit: int
