"""The delays module's public API: the sweep that turns finished journeys into
frozen delay decisions.

Everything the rest of the system may do with delays goes through this file
(ARCHITECTURE §3). The sweep is source-agnostic: anything satisfying
`ArrivalsSource` can drive it — the HSP client (a later PR), a Darwin-backed
source eventually, a fake in tests.

Boundary: delays owns delay_detections and the pricing decision; the journeys
module owns the journeys/tickets tables and their status machine, so the work
query and every status transition go through journeys.service.

Concurrency protocol (review-hardened): the status update is a CLAIM — the
guarded UPDATE's rowcount says whether this process won the journey. Claim
first, insert the detection second; ON CONFLICT DO NOTHING absorbs whatever
the claim cannot see. A process that loses either step stops and counts
`lost_race` — it never double-counts, never overwrites, and never retires a
journey another writer just decided.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

import psycopg

# Private aliases for the same reason as in journeys.service: the module
# objects must not be reachable through this namespace, or callers could climb
# past every import-linter contract.
from autotrain.modules.delays import repository as _repository
from autotrain.modules.delays.models import ArrivalReport, Band
from autotrain.modules.journeys import service as _journeys

# Re-exported names: ArrivalReport/ArrivalsSource are the protocol surface a
# source implementation (or test fake) builds against; AssessableJourney is
# the row shape journeys.service hands the sweep; Band feeds
# compute_entitlement. All importable from here by design.
from autotrain.modules.journeys.service import AssessableJourney

logger = logging.getLogger(__name__)

__all__ = [
    "ArrivalReport",
    "ArrivalsSource",
    "AssessableJourney",
    "Band",
    "SweepStats",
    "compute_entitlement",
    "run_sweep",
]


class ArrivalsSource(Protocol):
    """Anything that can report what actually happened to a journey.

    Returns None when it does not know (yet) — the journey stays in the sweep
    until data appears or the give-up window passes. Implementations may
    raise; the sweep isolates the failure to that one journey.
    """

    def actual_arrival(self, journey: AssessableJourney) -> ArrivalReport | None: ...


@dataclass
class SweepStats:
    """One sweep's outcome, for the ingestor's log line."""

    examined: int = 0
    assessed: int = 0  # decision written; journey closed
    entitled: int = 0  # subset of assessed with entitlement_pence > 0
    no_data: int = 0  # source knew nothing; journey left for the next sweep
    gave_up: int = 0  # no data within the window; pending journey retired
    lost_race: int = 0  # another process decided this journey first
    errors: int = 0  # journey failed and was rolled back; sweep continued


def compute_entitlement(
    bands: Sequence[Band],
    *,
    delay_minutes: int,
    min_delay_minutes: int,
    price_pence: int,
    ticket_kind: str,
) -> tuple[int | None, int]:
    """The frozen pricing decision: (band_percent, entitlement_pence).

    National Delay Repay conventions:
    * below the operator's minimum → no entitlement at all;
    * singles: the band percentage of the ticket price;
    * returns: one delayed leg is compensated on HALF the return price,
      except bands flagged of_return_fare (the 120+ band), which pay on the
      full return price;
    * seasons: never priced — and never closed at zero either: the work query
      excludes them entirely, so a season journey is deferred, not forfeited.
      This guard is defence in depth for direct callers;
    * integer floor division — a claim is never rounded up.
    """
    if delay_minutes < min_delay_minutes:
        return (None, 0)
    if ticket_kind == "season":
        return (None, 0)

    for band in bands:
        if band.min_minutes <= delay_minutes and (
            band.max_minutes is None or delay_minutes < band.max_minutes
        ):
            if ticket_kind == "return" and not band.of_return_fare:
                applicable = price_pence // 2
            else:
                applicable = price_pence
            return (band.percent, applicable * band.percent // 100)

    # Past the operator minimum but inside no band (a scheme with a gap, or an
    # operator with no bands seeded): record the delay, entitle nothing.
    return (None, 0)


def run_sweep(
    conn: psycopg.Connection,
    source: ArrivalsSource,
    *,
    cutoff: datetime,
    give_up_before: date,
    batch_size: int = 200,
    commit_each: bool = False,
    abort_error_streak: int = 25,
) -> SweepStats:
    """Assess every journey whose scheduled arrival is before `cutoff`.

    Pages through the whole backlog (keyset cursor), `batch_size` rows at a
    time, so journeys the source cannot answer for yet never starve the ones
    behind them.

    Transactions: each journey runs inside its own nested transaction
    (savepoint) so one bad journey rolls back alone. With `commit_each=False`
    (default) everything stays inside the caller's transaction — what tests
    and composing callers want. The ingestor passes True, handing the sweep
    ownership of transaction boundaries: every decision commits as soon as it
    is made, so locks release immediately, finished work survives a
    mid-sweep crash, and concurrent readers see decisions without waiting for
    the whole sweep.

    Failure containment: a journey whose source call RAISES is retired (aged
    out) only after this sweep proves the source alive for someone else — if
    every call failed, the problem is the source (bad credentials, outage),
    not the journeys, and retiring them would permanently forfeit real
    claims. For the same reason the sweep aborts once its first
    `abort_error_streak` journeys all error: no point paying a timeout per
    journey to learn the source is down.
    """
    stats = SweepStats()
    retire_after: list[AssessableJourney] = []
    after: tuple[date, datetime, UUID] | None = None
    aborted = False
    while True:
        page = _journeys.list_awaiting_assessment(conn, cutoff, batch_size, after)
        if not page:
            break
        for journey in page:
            stats.examined += 1
            try:
                with conn.transaction():
                    _assess_one(conn, source, journey, give_up_before, stats)
            except Exception:
                logger.exception("sweep: journey %s failed; continuing", journey.id)
                stats.errors += 1
                # Poison-journey candidate: a journey that makes the source
                # RAISE must still age out eventually, or it occupies the
                # sweep forever. Decided after the loop, once we know the
                # failures were journey-scoped. ('matched' journeys never
                # retire — 0005 semantics — so a matched poison journey
                # retries forever; acceptable until a matcher exists and
                # brings its own terminal state.)
                if journey.status == "pending" and journey.travel_date <= give_up_before:
                    retire_after.append(journey)
                if stats.errors == stats.examined and stats.errors >= abort_error_streak:
                    logger.error(
                        "sweep: first %d journeys all failed — treating the source "
                        "as down and aborting this sweep",
                        stats.errors,
                    )
                    aborted = True
                    break
            if commit_each:
                conn.commit()
        if aborted:
            break
        last = page[-1]
        after = (last.travel_date, last.scheduled_arrival, last.id)
        if len(page) < batch_size:
            break

    if stats.examined > stats.errors:
        # At least one journey got a real answer, so the source is alive and
        # the failures above are journey-scoped: age the old ones out.
        for journey in retire_after:
            with conn.transaction():
                retired = _journeys.mark_unmatched(conn, journey.id)
            if retired:
                stats.gave_up += 1
            if commit_each:
                conn.commit()
    return stats


def _assess_one(
    conn: psycopg.Connection,
    source: ArrivalsSource,
    journey: AssessableJourney,
    give_up_before: date,
    stats: SweepStats,
) -> None:
    report = source.actual_arrival(journey)

    if report is None:
        # <= so the config knob means what it says: travelled exactly
        # give_up_days ago = retired. Only 'pending' journeys retire —
        # 'unmatched' means "we gave up matching" (0005), which can never be
        # true of a journey already matched to a service.
        if journey.status == "pending" and journey.travel_date <= give_up_before:
            if _journeys.mark_unmatched(conn, journey.id):
                stats.gave_up += 1
            else:
                stats.lost_race += 1
        else:
            stats.no_data += 1
        return

    delay_minutes = _minutes_late(journey.scheduled_arrival, report.actual_arrival)

    operator_id = journey.operator_id
    min_delay = journey.operator_min_delay_minutes
    if operator_id is None and report.atoc_code is not None:
        ref = _repository.operator_by_atoc(conn, report.atoc_code)
        if ref is not None:
            operator_id, min_delay = ref.id, ref.min_delay_minutes
            _journeys.assign_operator(conn, journey.id, ref.id)

    if operator_id is None or min_delay is None:
        # No scheme to price against. Record the observation so the journey
        # stops being swept, but entitle nothing. (min_delay is None exactly
        # when operator_id is None — both come from the same LEFT JOIN — the
        # double test is for the type checker.)
        band_percent, entitlement = None, 0
    else:
        bands = _repository.bands_for_operator(conn, operator_id)
        band_percent, entitlement = compute_entitlement(
            bands,
            delay_minutes=delay_minutes,
            min_delay_minutes=min_delay,
            price_pence=journey.price_pence,
            ticket_kind=journey.ticket_kind,
        )

    # Claim first (guarded UPDATE, rowcount = did we win), write second. A
    # loser stops here, so two overlapping sweeps can never both count — or
    # both notify — the same journey, and a give-up can never race a
    # detection into an 'unmatched' journey.
    if not _journeys.mark_assessed(conn, journey.id):
        stats.lost_race += 1
        return
    if not _repository.insert_detection(
        conn,
        journey_id=journey.id,
        actual_arrival=report.actual_arrival,
        delay_minutes=delay_minutes,
        source=report.source,
        band_percent=band_percent,
        entitlement_pence=entitlement,
    ):
        # Detection existed while the journey was still open — a writer that
        # crashed between its insert and its claim. Keep our status repair,
        # but the frozen decision is theirs, not ours.
        logger.warning("journey %s: detection already existed; kept it", journey.id)
        stats.lost_race += 1
        return
    stats.assessed += 1
    if entitlement > 0:
        stats.entitled += 1


def _minutes_late(scheduled: datetime, actual: datetime) -> int:
    """Whole minutes late, floored; early or on time is 0. Delay Repay bands
    are defined in whole minutes — 29m59s is a 29-minute delay."""
    seconds = (actual - scheduled).total_seconds()
    return max(0, int(seconds // 60))
