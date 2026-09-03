"""Wire shapes for the journeys and claims endpoints.

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


class DecisionOut(BaseModel):
    """A journey's frozen delay decision. Built straight from a DelayDecision."""

    model_config = ConfigDict(from_attributes=True)

    actual_arrival: AwareDatetime
    delay_minutes: int
    source: str
    band_percent: int | None
    entitlement_pence: int
    observed_at: AwareDatetime

    @field_serializer("actual_arrival", "observed_at")
    def _in_utc(self, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ClaimOut(BaseModel):
    """A claim as the API reports it. Built straight from a ClaimRow.

    user_id (it is the caller), submission_token (an internal idempotency
    key for operator submission) and detection_id (internal linkage) stay
    off the wire deliberately.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    journey_id: UUID
    operator_id: UUID
    amount_pence: int
    status: str
    file_by: date
    submitted_at: AwareDatetime | None
    resolved_at: AwareDatetime | None
    operator_reference: str | None
    created_at: AwareDatetime

    @field_serializer("submitted_at", "resolved_at", "created_at")
    def _in_utc(self, value: datetime | None) -> datetime | None:
        # Unlike JourneyOut's serializer these fields can be NULL (a claim
        # not yet submitted or resolved), so None passes through.
        return value.astimezone(UTC) if value is not None else None


class ClaimPage(BaseModel):
    """One page of a user's claims, newest first."""

    items: list[ClaimOut]
    count: int
    limit: int


class ClaimEventOut(BaseModel):
    """One audited state transition. from_status None is the creation event."""

    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    detail: str | None
    created_at: AwareDatetime

    @field_serializer("created_at")
    def _in_utc(self, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ClaimSummaryOut(BaseModel):
    """money recovered box on the home screen"""

    model_config = ConfigDict(from_attributes=True)

    recovered_pence: int
    pending_pence: int


class ClaimFilingOut(BaseModel):
    """The deep-link handoff: where the user files this claim, and the status
    it was left in. Built straight from a ClaimFiling."""

    model_config = ConfigDict(from_attributes=True)

    url: str
    status: str


class LoginRequest(BaseModel):
    """A request to log in, which the service turns into a one-time token."""

    email: str


class LoginVerify(BaseModel):
    """A one-time token the service turns into a session token."""

    token: str


class SessionOut(BaseModel):
    """A session token the client can use to authenticate future requests."""

    access_token: str
