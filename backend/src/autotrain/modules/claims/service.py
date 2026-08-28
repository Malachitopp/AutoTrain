"""The claims module's public API: turning a frozen delay decision into a
Delay Repay claim, and moving that claim through its audited state machine.

Everything the rest of the system may do with claims goes through this file
(ARCHITECTURE §3). Boundary: claims owns `claims` and `claim_events` only. The
detection being claimed belongs to delays and the journey behind it belongs to
journeys, so both arrive through their own services — the same shape as the
delay sweep driving journey status transitions through journeys.service.

Why a sweep and not an event. ARCHITECTURE §3 has delays emit `DelayDetected`
for claims to subscribe to, and that bus is in-process today. An in-process
event is lost if the process dies between committing the detection and
dispatching it — and a lost event here is a claim never filed, which is a
user's money. So the correctness path is this sweep, driven by
`claims_processed_at` (0010) and made idempotent by the one-claim-per-journey
constraint (0006). When the event bus lands it can call `open_claim` to cut
latency; it will not become the thing that guarantees the claim exists.

Not here yet, deliberately: the per-operator adapters (§6) and the consent gate
on auto-filing (`users.claim_consent_at`). Both belong at the point of FILING,
not creation — and consent lives in the identity module, which has no code to
route the question through yet. A claim is created for every entitled
detection; whether we may file it on the user's behalf is the next question,
not this one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

import psycopg

# Private aliases for the same reason as in the other module services: the
# module objects must not be reachable through this namespace, or callers could
# climb past every import-linter contract.
from autotrain.modules.claims import repository as _repository
from autotrain.modules.claims.models import ClaimEventRow, ClaimRow, ClaimTotal
from autotrain.modules.delays import service as _delays

# Re-exported: the two row shapes a claim is built from. Callers get them from
# here, so writing against claims needs exactly one import.
from autotrain.modules.delays.service import UnclaimedDetection
from autotrain.modules.journeys import service as _journeys
from autotrain.modules.journeys.service import ClaimContext

logger = logging.getLogger(__name__)

__all__ = [
    "LEGAL_TRANSITIONS",
    "ClaimContext",
    "ClaimEventRow",
    "ClaimRow",
    "ClaimSweepStats",
    "ClaimTotal",
    "ClaimsError",
    "IllegalTransition",
    "NotClaimable",
    "UnclaimedDetection",
    "UnknownClaim",
    "claim_history",
    "claims_total",
    "expire_overdue",
    "get_claim",
    "list_claims",
    "open_claim",
    "run_claim_sweep",
    "transition",
]

# The claim state machine. 0006 defines the set of states; legality of a move
# is this layer's job, because "trigger-enforced state machines are miserable
# to evolve" and the audit trail is what actually matters.
#
#   draft -> ready -> submitted -> approved -> paid
#                          `-> rejected
#   draft/ready <-> needs_user -> submitted
#   draft/ready/needs_user -> expired
#
# Notes on the less obvious edges:
#   * ready -> needs_user is the §6 failure policy: after N adapter failures a
#     claim parks and the user gets the deep-link fallback — degraded, never
#     dropped. needs_user -> ready is the way back once the adapter is fixed.
#   * submitted -> paid skips 'approved': plenty of operators simply pay,
#     without ever sending an approval we can observe.
#   * expiry is about the FILING deadline, so only states that have not been
#     filed yet can reach it.
#   * paid / rejected / expired are terminal — an empty set, not a missing key.
LEGAL_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "draft": frozenset({"ready", "needs_user", "expired"}),
    "ready": frozenset({"submitted", "needs_user", "expired"}),
    "needs_user": frozenset({"ready", "submitted", "expired"}),
    "submitted": frozenset({"approved", "rejected", "paid"}),
    "approved": frozenset({"paid"}),
    "rejected": frozenset(),
    "paid": frozenset(),
    "expired": frozenset(),
}


class ClaimsError(Exception):
    """Base for claims domain failures."""


class UnknownClaim(ClaimsError):
    """No claim with that id."""


class IllegalTransition(ClaimsError):
    """The state machine has no edge from the claim's current status to the
    requested one. A programming error, not a race — losing a race is reported
    by a False return, not by this."""


class NotClaimable(ClaimsError):
    """The journey has no operator, so there is no scheme to file against and
    claims.operator_id (NOT NULL) cannot be satisfied. The sweep filters these
    out before calling; this exists so direct callers get a domain error."""


@dataclass
class ClaimSweepStats:
    """One claim-creation sweep's outcome, for the scheduler's log line."""

    # Examinations, not distinct detections: a detection that raises stays
    # in the work queue and is examined again on the next page.
    examined: int = 0
    opened: int = 0  # a new claim row exists because of this sweep
    already_claimed: int = 0  # another sweep created it first
    no_operator: int = 0  # unclaimable; stamped so it leaves the work queue
    errors: int = 0  # detection failed and was rolled back; sweep continued


def open_claim(
    conn: psycopg.Connection, detection: UnclaimedDetection, context: ClaimContext
) -> ClaimRow | None:
    """Create the draft claim for one entitled detection, idempotently.

    Returns the new claim, or None when one already existed for this journey.
    The caller owns the transaction, so the insert and its creation event
    commit together or not at all.

    The amount is the detection's frozen entitlement, copied rather than
    recomputed: the claim must reflect the scheme at time of travel (0006), and
    an operator that later approves a different figure is recorded as a
    transition, never by editing the amount.

    `file_by` is frozen here too, from the operator's window at creation time.
    A back-claim whose window has ALREADY closed is still created, and left for
    the deadline sweep to expire — the user is owed an explanation of what
    happened to their money, and silence is not one.
    """
    if context.operator_id is None or context.claim_window_days is None:
        raise NotClaimable(f"journey {context.journey_id} has no operator")

    claim = _repository.insert_claim(
        conn,
        journey_id=context.journey_id,
        detection_id=detection.id,
        operator_id=context.operator_id,
        user_id=context.user_id,
        amount_pence=detection.entitlement_pence,
        file_by=context.travel_date + timedelta(days=context.claim_window_days),
    )
    if claim is None:
        return None
    # from_status NULL is the creation event (0006). Written here rather than by
    # a trigger, so claim_events has exactly one writer.
    _repository.insert_event(
        conn, claim_id=claim.id, from_status=None, to_status=claim.status, detail=None
    )
    return claim


def transition(
    conn: psycopg.Connection, claim_id: UUID, to_status: str, *, detail: str | None = None
) -> bool:
    """Move a claim to `to_status`, recording the move in claim_events.

    True if this call moved it. False means another writer moved the claim
    between our read and our write, and the caller must stop — the same
    rowcount-is-load-bearing protocol as the delay sweep's claims. An edge the
    machine does not have raises IllegalTransition instead: that is a bug in the
    caller, and silently returning False would hide it.

    submitted_at and resolved_at are derived from the destination state inside
    the UPDATE, so they can never disagree with `status`.
    """
    claim = _repository.get_by_id(conn, claim_id)
    if claim is None:
        raise UnknownClaim(str(claim_id))

    # .get, not [...]: a status added by a future migration without an entry
    # here should raise IllegalTransition, not KeyError.
    if to_status not in LEGAL_TRANSITIONS.get(claim.status, frozenset()):
        raise IllegalTransition(f"{claim.status} -> {to_status} is not a legal claim transition")

    if not _repository.transition(
        conn, claim_id=claim_id, from_status=claim.status, to_status=to_status
    ):
        return False
    _repository.insert_event(
        conn, claim_id=claim_id, from_status=claim.status, to_status=to_status, detail=detail
    )
    return True


def run_claim_sweep(
    conn: psycopg.Connection, *, batch_size: int = 200, commit_each: bool = False
) -> ClaimSweepStats:
    """Open a claim for every entitled detection that does not have one.

    Idempotent by construction: the work query returns only detections claims
    has not decided about (0010), and the one-claim-per-journey constraint
    absorbs anything two concurrent sweeps both reach.

    No keyset cursor, unlike the delay sweep: every detection examined here is
    stamped processed, so the work set strictly shrinks and the head always
    advances. The one thing that can stall it is a page in which every row
    raises, which is what the no-progress guard below catches.
    """
    stats = ClaimSweepStats()
    while True:
        page = _delays.list_unclaimed_detections(conn, batch_size)
        if not page:
            break
        contexts = _journeys.claim_contexts(conn, [d.journey_id for d in page])

        progressed = 0
        for detection in page:
            stats.examined += 1
            try:
                with conn.transaction():
                    _open_one(conn, detection, contexts.get(detection.journey_id), stats)
                progressed += 1
            except Exception:
                logger.exception("claim sweep: detection %s failed; continuing", detection.id)
                stats.errors += 1
            if commit_each:
                conn.commit()

        if progressed == 0:
            # Nothing left the queue, so the next page would be this page again.
            logger.error(
                "claim sweep: all %d detections in the page failed — stopping rather "
                "than looping on them",
                len(page),
            )
            break
        if len(page) < batch_size:
            break
    return stats


def _open_one(
    conn: psycopg.Connection,
    detection: UnclaimedDetection,
    context: ClaimContext | None,
    stats: ClaimSweepStats,
) -> None:
    if context is None or context.operator_id is None:
        # No operator means nothing to file against, and that will not change:
        # the journey has already been assessed. Stamp it so it leaves the queue
        # rather than being re-examined by every future sweep — the same
        # poison-row failure the delay sweep had to fix. The absent claims row
        # is the record that we never filed.
        #
        # (context None is effectively unreachable: delay_detections cascades
        # from journeys, so a deleted journey takes its detection with it.)
        logger.info("claim sweep: detection %s has no operator; not claimable", detection.id)
        stats.no_operator += 1
        _delays.mark_claims_processed(conn, detection.id)
        return

    # Money-bearing write first, work marker second. They commit together, so
    # the order is not what makes this safe — the idempotent insert is — but an
    # interrupted sweep that wrote neither is the state we want to retry from.
    if open_claim(conn, detection, context) is None:
        stats.already_claimed += 1
    else:
        stats.opened += 1
    _delays.mark_claims_processed(conn, detection.id)


def expire_overdue(
    conn: psycopg.Connection, today: date, *, batch_size: int = 200, commit_each: bool = False
) -> int:
    """Expire claims whose filing window closed before `today`, returning how
    many this call expired.

    Each one goes through `transition`, so an expiry is audited in claim_events
    exactly like every other state change — worth the extra read per claim, on a
    job that runs nightly over a handful of rows.

    'needs_user' claims are deliberately untouched: the user holds a deep link
    and may have filed it themselves, so we do not know the claim is dead and
    will not tell them it is.
    """
    expired = 0
    while True:
        page = _repository.list_overdue(conn, today, batch_size)
        if not page:
            break

        progressed = 0
        for claim_id, status in page:
            try:
                with conn.transaction():
                    moved = transition(
                        conn,
                        claim_id,
                        "expired",
                        detail=f"deadline sweep {today.isoformat()}: filing window had closed",
                    )
                if moved:
                    expired += 1
                progressed += 1
            except ClaimsError:
                # An IllegalTransition here means the status moved out of
                # draft/ready between the work query and the read inside
                # transition() — a race, not a bug, and the row has left the
                # work query on its own. Deliberately NOT counted as progress:
                # if a future migration adds a status to _LIST_OVERDUE with no
                # edge to 'expired', every row raises, and the guard below is
                # what stops this looping on them forever.
                logger.exception("claim expiry: claim %s (%s) could not expire", claim_id, status)
            if commit_each:
                conn.commit()

        if progressed == 0:
            logger.error("claim expiry: no progress on a page of %d; stopping", len(page))
            break
        if len(page) < batch_size:
            break
    return expired


# --- Read paths -------------------------------------------------------------


def get_claim(conn: psycopg.Connection, claim_id: UUID, user_id: UUID) -> ClaimRow | None:
    """The claim, or None when absent — including "exists but is not yours":
    ownership is part of the lookup, so existence never leaks across users."""
    return _repository.get_for_user(conn, claim_id, user_id)


def list_claims(conn: psycopg.Connection, user_id: UUID, limit: int = 50) -> list[ClaimRow]:
    """The user's claims, newest first."""
    return _repository.list_for_user(conn, user_id, limit)


def claim_history(conn: psycopg.Connection, claim_id: UUID) -> list[ClaimEventRow]:
    """Every recorded transition for a claim, oldest first — the audit of what
    we did on the user's behalf and when (0006)."""
    return _repository.events_for_claim(conn, claim_id)


def claims_total(conn: psycopg.Connection, user_id: UUID) -> ClaimTotal:
    """Returns the total claim amount for the user"""
    return _repository.totals_for_user(conn, user_id)
