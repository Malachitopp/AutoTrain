"""Reading a forwarded ticket email into journeys — the messy-input boundary
of ARCHITECTURE §6a, kept in one file.

The split is the one §6a prescribes: an LLM turns "any retailer's
confirmation email" into ONE structured shape (ExtractedTicket, below), and
deterministic code decides whether that shape is trustworthy enough to act
on. The reader is behind a Protocol (TicketExtractor) so tests use a scripted
one and the real one (sources/ticket_emails.py) is built by the entrypoint —
the same seam as delays' ArrivalsSource.

Every guard here fails towards a person: anything the reader is unsure of,
or that the checks below cannot verify, lands in 'needs_review' with a plain
reason, and never becomes a journey by guesswork. Money is computed
downstream from these rows, so a wrong journey is worse than a missing one.

Two things the reader says are recorded but never acted on:
* the operator. 0005's rule: who SOLD the ticket is not who RAN the train,
  and a booking email is branded by the seller. The delay sweep assigns the
  operator from the arrivals data, which knows.
* several tickets in one email. The schema holds one ticket with one price,
  and the entitlement calculator prices every journey on a 'single' at the
  full price — so a "single" with two legs would be paid twice over. The
  legs-per-kind check below sends that to a person instead.

Journeys-internal: the journeys-privacy contract forbids importing this file
from outside the module. Callers reach it through service.run_intake_sweep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

_LONDON = ZoneInfo("Europe/London")

# A booking more than this far ahead is almost certainly a misread date
# (tickets go on sale ~12 weeks ahead; a year covers every advance scheme
# with room to spare).
_MAX_DAYS_AHEAD = 400
# ...and a journey this long ago cannot be claimed (Delay Repay windows are
# 28 days), so a date this far back is either a misread year or nothing we
# can act on. Either way a person decides, not the delay sweep.
_MAX_DAYS_BEHIND = 60

# How many legs each ticket kind can carry: a single is one train (a change
# of trains is still one leg, first departure to final arrival); a return is
# out and back, or just out when the return is open.
_LEGS_PER_KIND = {"single": (1, 1), "return": (1, 2)}

_CRS = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")


# --- What the reader produces --------------------------------------------------
# Pydantic on purpose (the rest of the module uses dataclasses): this IS the
# schema handed to the model for structured output, and the validation of the
# model's answer against it. Strings for dates and times rather than date/time
# types: the model writes them, our code parses them, and a parse failure is a
# named review reason instead of a schema error deep in a library.


class ExtractedLeg(BaseModel):
    """One train the ticket covers, as the email states it. Times are London
    wall-clock, exactly as printed; conversion to UTC happens here, not in
    the reader. The operator fields are kept for whoever reviews the row;
    nothing is decided from them (module docstring)."""

    origin_crs: str = Field(description="Three-letter CRS code of the departure station")
    destination_crs: str = Field(description="Three-letter CRS code of the arrival station")
    travel_date: str = Field(description="Date of travel, YYYY-MM-DD")
    departure_time: str = Field(description="Scheduled departure, HH:MM, 24-hour, UK local time")
    arrival_time: str | None = Field(
        description=(
            "Scheduled arrival, HH:MM, 24-hour, UK local time; null if the email does not say"
        )
    )
    operator_name: str | None = Field(description="The train operator as printed, or null")
    operator_atoc_code: str | None = Field(
        description="The operator's two-letter ATOC code if known with certainty, else null"
    )


class ExtractedTicket(BaseModel):
    """The reader's whole answer about one email: one ticket, one price."""

    is_ticket: bool = Field(
        description="True only for a booking confirmation or e-ticket for a UK rail journey"
    )
    retailer: str | None = Field(description="Who sold the ticket, as printed (Trainline, LNER...)")
    kind: Literal["single", "return", "season", "unknown"] = Field(
        description="Ticket type; 'unknown' when the email does not make it clear"
    )
    price_pence: int | None = Field(
        description="Total paid for the ticket in pence (e.g. £24.50 -> 2450); null if not shown"
    )
    legs: list[ExtractedLeg] = Field(description="Every train covered, in travel order")
    confidence: Literal["high", "low"] = Field(
        description="'low' if any field above was inferred rather than read from the email"
    )
    notes: str | None = Field(description="Anything a reviewer should know; null if nothing")


class TicketExtractor(Protocol):
    """Anything that can read one ticket email into an ExtractedTicket.

    Implemented outside the module (sources/ticket_emails.py) and handed in
    by the scheduler. `received_at` is when the email reached us: the one
    date the reader can anchor a year to, since forwarded bodies often print
    "Sat 12 Sep" and nothing more. May raise: the sweep isolates the failure
    to that one email and tries again on a later sweep, up to a limit. Must
    bound its own time (network timeouts) — a sweep is waiting on it.
    """

    def extract(self, *, subject: str, body: str, received_at: datetime) -> ExtractedTicket: ...


# --- What the checks decide -----------------------------------------------------


@dataclass(frozen=True)
class LegSpec:
    """A leg the checks have passed: typed, in UTC, ready to insert. Also the
    shape the manual add uses, so both ways in share one writer."""

    origin_crs: str
    destination_crs: str
    travel_date: date
    scheduled_departure: datetime
    scheduled_arrival: datetime


@dataclass(frozen=True)
class TicketPlan:
    """Everything the writer needs for a ticket the checks have passed."""

    kind: str
    price_pence: int
    retailer: str | None
    legs: tuple[LegSpec, ...]


@dataclass(frozen=True)
class Verdict:
    """The deterministic half's answer. `plan` is set only for 'proceed';
    every other status carries the reason a person would need."""

    status: Literal["proceed", "needs_review", "rejected"]
    reason: str | None = None
    plan: TicketPlan | None = None


def judge(ticket: ExtractedTicket, *, today: date) -> Verdict:
    """Decide what the reader's answer allows. Pure: no database; `today` is
    passed in (the date-range checks) so tests are not tied to the clock.

    Order matters only for which reason is reported; every path that is not
    'proceed' creates nothing.
    """
    if not ticket.is_ticket:
        return Verdict("rejected", "not a ticket email")
    if ticket.kind == "season":
        # Season tickets are not monitored (the delay sweep skips them), so
        # there is nothing to create and nothing to review.
        return Verdict("rejected", "season ticket: not monitored")
    if ticket.confidence != "high":
        return Verdict("needs_review", f"reader unsure: {ticket.notes or 'no detail given'}")
    if ticket.kind == "unknown":
        return Verdict("needs_review", "ticket type unclear")
    if ticket.price_pence is None:
        return Verdict("needs_review", "price not found")
    if ticket.price_pence < 0:
        return Verdict("needs_review", "negative price")
    if not ticket.legs:
        return Verdict("needs_review", "no journeys found in the email")

    fewest, most = _LEGS_PER_KIND[ticket.kind]
    if not fewest <= len(ticket.legs) <= most:
        # One price for several trains that are not out-and-back means
        # several tickets in one email, and the calculator would pay the
        # whole price on each. Not ours to split.
        return Verdict(
            "needs_review",
            f"{len(ticket.legs)} legs on a {ticket.kind} ticket: several tickets in one email?",
        )

    legs: list[LegSpec] = []
    for index, leg in enumerate(ticket.legs, start=1):
        try:
            spec = _check_leg(leg, today=today)
        except ValueError as exc:
            return Verdict("needs_review", f"leg {index}: {exc}")
        for earlier, other in enumerate(legs, start=1):
            if _same_train(spec, other):
                return Verdict("needs_review", f"leg {index} repeats leg {earlier}")
        legs.append(spec)

    plan = TicketPlan(
        kind=ticket.kind, price_pence=ticket.price_pence, retailer=ticket.retailer, legs=tuple(legs)
    )
    return Verdict("proceed", None, plan)


def _same_train(a: LegSpec, b: LegSpec) -> bool:
    return (a.travel_date, a.origin_crs, a.destination_crs, a.scheduled_departure) == (
        b.travel_date,
        b.origin_crs,
        b.destination_crs,
        b.scheduled_departure,
    )


def _check_leg(leg: ExtractedLeg, *, today: date) -> LegSpec:
    """Type one leg, or raise ValueError with the reason a reviewer needs."""
    if not _CRS.match(leg.origin_crs):
        raise ValueError(f"origin {leg.origin_crs!r} is not a station code")
    if not _CRS.match(leg.destination_crs):
        raise ValueError(f"destination {leg.destination_crs!r} is not a station code")
    if leg.origin_crs == leg.destination_crs:
        raise ValueError("origin and destination are the same station")

    travel_date = _parse_date(leg.travel_date)
    if travel_date > today + timedelta(days=_MAX_DAYS_AHEAD):
        raise ValueError(f"travel date {leg.travel_date} is too far ahead")
    if travel_date < today - timedelta(days=_MAX_DAYS_BEHIND):
        raise ValueError(f"travel date {leg.travel_date} is too far in the past to claim")
    if leg.arrival_time is None:
        raise ValueError("arrival time not found")

    departure = _london_instant(travel_date, _parse_time(leg.departure_time, "departure"))
    arrival = _london_instant(travel_date, _parse_time(leg.arrival_time, "arrival"))
    if arrival < departure:
        # Printed times are wall-clock; an arrival before the departure is
        # after midnight, the next day. Equal times stay a misread.
        arrival += timedelta(days=1)
    if arrival == departure:
        raise ValueError("arrival time equals departure time")

    return LegSpec(
        origin_crs=leg.origin_crs,
        destination_crs=leg.destination_crs,
        travel_date=travel_date,
        scheduled_departure=departure,
        scheduled_arrival=arrival,
    )


def _parse_date(text: str) -> date:
    if not _DATE.match(text):
        raise ValueError(f"date {text!r} is not YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"date {text!r} is not a real date") from exc


def _parse_time(text: str, label: str) -> time:
    if not _TIME.match(text):
        raise ValueError(f"{label} time {text!r} is not HH:MM")
    try:
        return time.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} time {text!r} is not a real time") from exc


def _london_instant(day: date, wall_clock: time) -> datetime:
    """A UK wall-clock reading as a UTC instant. On the autumn night the
    clocks go back, 01:00 to 01:59 happens twice; fold=0 takes the first (BST)
    occurrence — one hour a year, and the delay sweep tolerates minutes."""
    return datetime.combine(day, wall_clock, tzinfo=_LONDON).astimezone(UTC)
