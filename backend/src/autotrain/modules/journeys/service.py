"""The journeys module's public API.

Everything the rest of the system may do with journeys goes through this file
(ARCHITECTURE §3): the api layer and other modules import it and nothing else
in the module. psycopg's exception types never escape it either — database
failures surface as the domain exceptions below, so callers stay free of
driver knowledge.

Two ways in, one writer. add_manual_journey takes the form's fields; the
intake functions take a forwarded ticket email, hand it to a TicketExtractor
(the reader, built outside the module — see intake.py) and turn a trusted
reading into the same ticket + journeys through the same private writer, so
nothing downstream can tell how a journey arrived.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

# Private alias on purpose: a plain `import repository` would bind the module
# onto this namespace, letting `from ...journeys.service import repository`
# hand callers the repository past every import-linter contract (the graph
# records only an import of the service). The underscore makes that an
# ImportError, so the public surface is exactly the functions below.
from autotrain.modules.journeys import intake as _intake
from autotrain.modules.journeys import repository as _repository

# Re-exported deliberately: the reader's protocol and its answer shape, so the
# scheduler and the reader implementation import them from here and stay off
# journeys.intake (the journeys-privacy contract).
from autotrain.modules.journeys.intake import (
    ExtractedLeg,
    ExtractedTicket,
    Mailbox,
    MailboxAttachment,
    MailboxHeader,
    MailboxMessage,
    TicketExtractor,
)

# AssessableJourney, ClaimContext and NotificationContext are re-exported
# deliberately: they are the shapes this service hands the delay engine, the
# claims module and the notification worker, and importing them from here
# keeps callers off journeys.models (the journeys-privacy contract).
from autotrain.modules.journeys.models import (
    AssessableJourney,
    ClaimContext,
    InboundEmailRow,
    InboundEmailSummary,
    JourneyRow,
    NotificationContext,
)

__all__ = [
    "MAX_INTAKE_ATTEMPTS",
    "AssessableJourney",
    "ClaimContext",
    "DuplicateJourney",
    "ExtractedLeg",
    "ExtractedTicket",
    "InboundEmailRow",
    "InboundEmailSummary",
    "IntakeStats",
    "InvalidJourney",
    "JourneyRow",
    "JourneysError",
    "Mailbox",
    "MailboxAttachment",
    "MailboxHeader",
    "MailboxMessage",
    "MailboxStats",
    "NotificationContext",
    "TicketExtractor",
    "UnknownUser",
    "add_manual_journey",
    "assign_operator",
    "claim_contexts",
    "forget_user",
    "get_journey",
    "list_awaiting_assessment",
    "list_inbound_emails",
    "list_journeys",
    "mark_assessed",
    "mark_unmatched",
    "notification_contexts",
    "receive_ticket_email",
    "run_intake_sweep",
    "run_mailbox_poll",
    "run_retention_sweep",
]

logger = logging.getLogger(__name__)

# A reader that keeps failing on one email (a malformed body, a model outage
# that outlasts the retries) must not be asked for ever: after this many
# attempts the email is marked failed and leaves the queue.
MAX_INTAKE_ATTEMPTS = 3

# The two constraints that mean "this journey is already tracked":
# journeys_user_leg_departure_key (0009) is the cross-request guard — every
# manual add mints a fresh ticket, so only a user-scoped key can catch a
# double-add; journeys_ticket_leg_key (0005) guards re-adds of the SAME
# ticket, for future callers that reuse one (a return's second leg, barcode
# re-parse).
_DUPLICATE_CONSTRAINTS = frozenset({"journeys_user_leg_departure_key", "journeys_ticket_leg_key"})


class JourneysError(Exception):
    """Base for journeys domain failures."""


class DuplicateJourney(JourneysError):
    """The user already tracks this departure on this travel date."""


class UnknownUser(JourneysError):
    """The user id references no user row — possible while auth is a stub."""


class InvalidJourney(JourneysError):
    """The values violate a database-enforced invariant (price out of range,
    unordered times). The API schemas reject these at the edge; this exists
    for direct service callers, so driver exceptions still never escape."""


def add_manual_journey(
    conn: psycopg.Connection,
    user_id: UUID,
    *,
    kind: str,
    price_pence: int,
    origin_crs: str,
    destination_crs: str,
    travel_date: date,
    scheduled_departure: datetime,
    scheduled_arrival: datetime,
) -> JourneyRow:
    """Create the ticket and its journey as one unit of work.

    The caller owns the transaction, so the two inserts commit or roll back
    together — a journey can never exist without its ticket, and a failed
    journey insert leaves no orphaned ticket behind.

    Duplicate policy: the leg_exists pre-check answers the common case (the
    same trip re-submitted sequentially) before any insert, but the guarantee
    itself is journeys_user_leg_departure_key in the database — two concurrent
    adds both pass the pre-check, and the race loser's INSERT then hits the
    index and is translated to DuplicateJourney below. A duplicate can get a
    409 two ways; it can never get stored twice.
    """
    if _repository.leg_exists(
        conn, user_id, travel_date, origin_crs, destination_crs, scheduled_departure
    ):
        raise DuplicateJourney(
            f"{origin_crs}->{destination_crs} on {travel_date} at {scheduled_departure} "
            "is already tracked"
        )
    leg = _intake.LegSpec(
        origin_crs=origin_crs,
        destination_crs=destination_crs,
        travel_date=travel_date,
        scheduled_departure=scheduled_departure,
        scheduled_arrival=scheduled_arrival,
    )
    return _insert_ticket_and_journeys(
        conn, user_id, kind=kind, price_pence=price_pence, source="manual", legs=[leg]
    )[0]


def _insert_ticket_and_journeys(
    conn: psycopg.Connection,
    user_id: UUID,
    *,
    kind: str,
    price_pence: int,
    source: str,
    legs: Sequence[_intake.LegSpec],
    retailer: str | None = None,
    source_payload: str | None = None,
) -> list[JourneyRow]:
    """One ticket and its journeys, as one unit of work — the writer behind
    both ways in. The caller owns the transaction, so the inserts commit or
    roll back together: no journey without its ticket, no ticket without its
    journeys. Driver errors become the module's domain errors here.
    """
    try:
        ticket_id = _repository.insert_ticket(
            conn,
            user_id,
            kind,
            price_pence,
            source,
            retailer=retailer,
            source_payload=source_payload,
        )
        rows: list[JourneyRow] = []
        for leg in legs:
            # No operator from either way in: the delay sweep assigns it from
            # the arrivals data, which knows who ran the train (0005's
            # seller-is-not-operator rule; intake.py's module docstring).
            rows.append(
                _repository.insert_journey(
                    conn,
                    user_id=user_id,
                    ticket_id=ticket_id,
                    origin_crs=leg.origin_crs,
                    destination_crs=leg.destination_crs,
                    travel_date=leg.travel_date,
                    scheduled_departure=leg.scheduled_departure,
                    scheduled_arrival=leg.scheduled_arrival,
                )
            )
        return rows
    except pg_errors.ForeignKeyViolation as exc:
        # Only the users FK can fail here — no operator id is supplied by
        # either way in — so this is always "that user does not exist".
        raise UnknownUser(str(user_id)) from exc
    except pg_errors.UniqueViolation as exc:
        if exc.diag.constraint_name in _DUPLICATE_CONSTRAINTS:
            raise DuplicateJourney(f"{_describe(legs)} is already tracked") from exc
        raise
    except (pg_errors.CheckViolation, pg_errors.DataError) as exc:
        # CHECKs (times ordered, CRS shape, price >= 0) and range errors
        # (price_pence past int4). API callers never reach this — the schemas
        # mirror every one of these constraints — but a direct caller must
        # still get a domain error, not a driver class.
        raise InvalidJourney(str(exc).strip()) from exc


def _describe(legs: Sequence[_intake.LegSpec]) -> str:
    if len(legs) == 1:
        leg = legs[0]
        return (
            f"{leg.origin_crs}->{leg.destination_crs} on {leg.travel_date} "
            f"at {leg.scheduled_departure}"
        )
    return f"one of the {len(legs)} journeys on this ticket"


def get_journey(conn: psycopg.Connection, journey_id: UUID, user_id: UUID) -> JourneyRow | None:
    """The journey, or None when absent — including "exists but is not yours":
    ownership is part of the lookup, so existence never leaks across users."""
    return _repository.get_for_user(conn, journey_id, user_id)


def list_journeys(conn: psycopg.Connection, user_id: UUID, limit: int = 50) -> list[JourneyRow]:
    """The user's journeys, newest travel date first."""
    return _repository.list_for_user(conn, user_id, limit)


# --- Journey lifecycle for the delay engine -------------------------------
# journeys owns its tables and its status machine; the delays module drives
# these transitions through here rather than with its own SQL
# (ARCHITECTURE §3: no cross-module table access).


def list_awaiting_assessment(
    conn: psycopg.Connection,
    cutoff: datetime,
    limit: int,
    after: tuple[date, datetime, UUID] | None = None,
) -> list[AssessableJourney]:
    """Journeys past their scheduled arrival still needing a delay decision,
    oldest first. `after` is the keyset cursor — pass the last row's
    (travel_date, scheduled_departure-ordering key, id) to page past journeys
    the caller examined but could not decide."""
    return _repository.list_awaiting_assessment(conn, cutoff, limit, after)


def mark_assessed(conn: psycopg.Connection, journey_id: UUID) -> bool:
    """Claim the journey for assessment: True moves 'pending'/'matched' →
    'assessed'; False means another process moved it first and the caller
    must stop (the rowcount-is-load-bearing protocol, core.db)."""
    return _repository.mark_assessed(conn, journey_id)


def mark_unmatched(conn: psycopg.Connection, journey_id: UUID) -> bool:
    """Retire a 'pending' journey we gave up on. Guarded to 'pending' only —
    a 'matched' journey has a service, so lacking data is never a matching
    failure (0005's status semantics)."""
    return _repository.mark_unmatched(conn, journey_id)


def assign_operator(conn: psycopg.Connection, journey_id: UUID, operator_id: UUID) -> None:
    """Attach an operator to a journey that has none. Never overrides an
    existing match."""
    _repository.assign_operator(conn, journey_id, operator_id)


# --- Journey facts for the claims module -----------------------------------


def claim_contexts(
    conn: psycopg.Connection, journey_ids: Sequence[UUID]
) -> dict[UUID, ClaimContext]:
    """The user, operator, travel date and filing window for each journey,
    keyed by journey id. Batched so a claims sweep resolves a page in one
    round trip; ids with no journey are simply absent from the result."""
    return _repository.claim_contexts(conn, journey_ids)


def notification_contexts(
    conn: psycopg.Connection, journey_ids: Sequence[UUID]
) -> dict[UUID, NotificationContext]:
    """Who to tell and which train the news is about, for each journey, keyed
    by journey id — the notification worker's version of claim_contexts.
    Batched; ids with no journey are simply absent from the result."""
    return _repository.notification_contexts(conn, journey_ids)


# --- Ticket-email intake ------------------------------------------------------


@dataclass
class IntakeStats:
    """One intake sweep's outcome, for the scheduler's log line."""

    examined: int = 0
    parsed: int = 0  # journeys created
    needs_review: int = 0  # read, not trusted; a person decides
    rejected: int = 0  # not a ticket, or a kind we do not monitor
    duplicate: int = 0  # every journey on it was already tracked
    failed: int = 0  # the reader kept erroring; gave up after MAX_INTAKE_ATTEMPTS
    lost_race: int = 0  # another writer decided the email while this sweep held it
    errors: int = 0  # the reader raised this time; the email waits for the next sweep


class _AlreadyDecided(Exception):
    """Raised inside the write savepoint when the email's status was changed
    by someone else between the read and the write, so the savepoint rolls
    the ticket and journeys back with it."""


# The door's daily cap counts a rolling day, not a calendar one: a cap that
# reset at midnight would let one burst straddle it and count twice over.
_CAP_WINDOW = timedelta(hours=24)


def receive_ticket_email(
    conn: psycopg.Connection,
    *,
    user_id: UUID | None,
    account_email: str | None,
    message_id: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    spf_pass: bool | None = None,
    dkim_pass: bool | None = None,
    attachments: Sequence[MailboxAttachment] = (),
    daily_cap: int | None = 50,
) -> InboundEmailRow | None:
    """Store one forwarded email: for the intake sweep to read, or as
    refused at the door, with the reason.

    Returns None when this message_id was stored before (a webhook retry).
    `user_id` None means the recipient address belongs to nobody — most
    often an erased account whose forwarding rule is still running. The row
    records only that mail arrived for the code, and when: sender, subject
    and body are all dropped, because there is no owner left to erase them.

    An email the door refuses (intake.screen: a failed provider check, or
    the daily cap) is stored under its user without its body — the sender
    and subject stay, so they can see what was dropped and why — and never
    reaches the reader.

    `attachments` are kept only for an email that reaches the reader's
    queue — a refused one has its body dropped, and its files belong with
    it. Every operator's Delay Repay form wants a picture of the ticket, so
    this is where the proof of a claim starts existing. The webhook passes
    none: a provider posting JSON has nowhere to put a PDF, which is one of
    the reasons a mailbox is the better door.

    Gmail's forwarding-setup message is recognised and filed as
    'confirmation' with its code in the reason, never handed to the reader:
    it is how the user switches automatic forwarding on, so it has to reach
    them rather than be answered "not a ticket".
    """
    now = datetime.now(UTC)
    if user_id is None:
        return _repository.insert_inbound_email(
            conn,
            user_id=None,
            message_id=message_id,
            sender="",
            recipient=recipient,
            subject="",
            body="",
            spf_pass=spf_pass,
            dkim_pass=dkim_pass,
            forwarding=None,
            status="rejected",
            status_reason="unknown recipient",
            processed_at=now,
        )
    if account_email is None:
        raise ValueError("account_email is required when user_id is given")
    # Counted only when a cap can fire: `daily_cap` None means there is
    # nothing to compare against, and the query would be a round trip whose
    # answer is thrown away.
    recent = (
        _repository.count_received_since(conn, user_id, now - _CAP_WINDOW, daily_cap)
        if daily_cap is not None
        else 0
    )
    verdict = _intake.screen(
        sender=sender,
        account_email=account_email,
        spf_pass=spf_pass,
        dkim_pass=dkim_pass,
        recent_count=recent,
        daily_cap=daily_cap,
    )
    store = functools.partial(
        _repository.insert_inbound_email,
        conn,
        user_id=user_id,
        message_id=message_id,
        sender=sender,
        recipient=recipient,
        subject=subject,
        spf_pass=spf_pass,
        dkim_pass=dkim_pass,
        forwarding=verdict.forwarding,
    )
    if verdict.refused is not None:
        return store(body="", status="rejected", status_reason=verdict.refused, processed_at=now)
    setup = _intake.forwarding_confirmation(sender=sender, subject=subject, body=body)
    if setup is not None:
        asked = setup.requested_by or "an account"
        return store(
            body=body,
            status="confirmation",
            status_reason=(
                f"{asked} asked to forward mail here. "
                f"Enter code {setup.code} in Gmail to switch it on."
            ),
            processed_at=now,
        )
    row = store(body=body, status="received", status_reason=None, processed_at=None)
    if row is not None:
        _store_attachments(conn, row.id, attachments)
    return row


def _store_attachments(
    conn: psycopg.Connection, email_id: UUID, attachments: Sequence[MailboxAttachment]
) -> int:
    """Keep the files worth keeping off one email; how many were kept.

    The caps live in intake.keepable_attachments, and the excess is dropped
    silently on purpose: the words are what the reader needs, so an email
    that arrived with a 40 MB video must still become a journey.
    """
    kept = _intake.keepable_attachments(attachments)
    for attachment in kept:
        _repository.insert_attachment(
            conn,
            email_id,
            filename=attachment.filename,
            content_type=attachment.content_type.lower(),
            content=attachment.content,
        )
    return len(kept)


# How many message headers one pass will list. Not a tuning knob: it is the
# point past which a folder is too big for the "list everything, fetch what
# is new" shape to hold, and reaching it is logged rather than silently
# truncating. A hand-curated label never comes near it.
MAX_MAILBOX_LISTING = 5_000


@dataclass
class MailboxStats:
    """One mailbox poll's outcome, for the scheduler's log line."""

    listed: int = 0  # messages the mailbox offered inside the window
    fresh: int = 0  # of those, ones we had not stored before
    stored: int = 0  # queued for the reader
    refused: int = 0  # stored, but the door said no (see status_reason)
    files: int = 0  # attachments kept across every stored email
    vanished: int = 0  # listed, then gone before it could be fetched
    duplicate: int = 0  # another writer stored it between the two steps
    errors: int = 0  # the mailbox raised on this one; retried next pass


def run_mailbox_poll(
    conn: psycopg.Connection,
    mailbox: Mailbox,
    *,
    user_id: UUID,
    account_email: str,
    lookback_days: int = 14,
    limit: int = 200,
    commit_each: bool = False,
) -> MailboxStats:
    """Read one person's inbox into the intake queue.

    The webhook's counterpart (receive_ticket_email is still the writer both
    share): instead of a mail provider posting each message to us, we open
    the mailbox and take what is in it. That makes this a SINGLE-USER path —
    a mailbox belongs to one person, so whose it is has to be passed in.

    Idempotency is the stored message_id, not anything done to the mailbox.
    Implementations are forbidden from marking mail read or moving it
    (intake.Mailbox), so this can be pointed at an inbox a person is also
    reading, and a poll that crashes half way repeats harmlessly. The cost
    of that choice is re-listing the same window every pass, which is why
    the listing is headers only: `known_message_ids` drops what we already
    have, and a body crosses the network at most once.

    `lookback_days` is therefore the real bound on work, not `limit`: a
    message older than the window is never offered again, so anything that
    has to be re-imported must be moved back into the window by hand. It
    only needs to cover the gap a stopped poller could leave.

    Failures are isolated per message: one that raises is logged and left
    for the next pass, and the rest of the batch still lands. A message
    listed and then deleted before it could be fetched is counted, not an
    error — a person tidying their inbox mid-poll is normal.
    """
    stats = MailboxStats()
    since = (datetime.now(UTC) - timedelta(days=lookback_days)).date()
    # Listed wide, fetched narrow. `limit` bounds BODIES, not the listing:
    # capping the listing first would mean that once `limit` messages in the
    # window were already stored, the pass had nothing left to look at and
    # the older unimported ones behind them could never be reached — they
    # would sit there until they aged out of the window, permanently
    # invisible, while every pass reported a clean zero. Headers are a few
    # hundred bytes each, so listing the whole window and then choosing is
    # both correct and cheap.
    headers = mailbox.list_recent(since=since, limit=MAX_MAILBOX_LISTING)
    stats.listed = len(headers)
    if len(headers) >= MAX_MAILBOX_LISTING:
        logger.warning(
            "mailbox poll: the listing cap of %d was reached; messages older than the "
            "newest %d in the window cannot be seen. Narrow the folder or shorten "
            "AUTOTRAIN_MAILBOX_LOOKBACK_DAYS.",
            MAX_MAILBOX_LISTING,
            MAX_MAILBOX_LISTING,
        )

    # Two messages can carry one Message-ID (a copy filed in another folder,
    # a client that reuses one). Keeping the first occurrence means the
    # second is not fetched only to be refused by the unique index.
    by_id: dict[str, _intake.MailboxHeader] = {}
    for header in headers:
        by_id.setdefault(header.message_id, header)
    known = _repository.known_message_ids(conn, list(by_id))
    fresh = [header for message_id, header in by_id.items() if message_id not in known]
    stats.fresh = len(fresh)
    if len(fresh) > limit:
        # Oldest first, which is the order the listing already comes in:
        # taking the oldest unimported guarantees a backlog drains instead of
        # a fixed head of it being re-examined for ever. A new booking waits
        # at most a few intervals behind a backlog, which costs nothing —
        # claims are judged on travel dates, not on when we read the email.
        logger.info(
            "mailbox poll: %d new messages, fetching the oldest %d this pass",
            len(fresh),
            limit,
        )
        fresh = fresh[:limit]

    for header in fresh:
        try:
            with conn.transaction():
                _poll_one(
                    conn,
                    mailbox,
                    header,
                    user_id=user_id,
                    account_email=account_email,
                    stats=stats,
                )
        except Exception:
            # Broad on purpose: a mailbox is a network client, and its
            # exception types are contractually invisible here (the module
            # never imports sources/). The savepoint has already rolled this
            # message back; the next pass lists it again.
            logger.exception("mailbox poll: message %s failed; continuing", header.message_id)
            stats.errors += 1
            continue
        if commit_each:
            conn.commit()
    return stats


def _poll_one(
    conn: psycopg.Connection,
    mailbox: Mailbox,
    header: _intake.MailboxHeader,
    *,
    user_id: UUID,
    account_email: str,
    stats: MailboxStats,
) -> None:
    message = mailbox.fetch(header.uid)
    if message is None:
        stats.vanished += 1
        return
    row = receive_ticket_email(
        conn,
        user_id=user_id,
        account_email=account_email,
        message_id=message.message_id,
        sender=message.sender,
        recipient=message.recipient,
        subject=message.subject,
        body=message.body,
        attachments=message.attachments,
        # No daily cap on this path, deliberately. The webhook's cap bounds
        # what a stranger who found a leaked forwarding address can make the
        # reader spend; a mailbox is one WE opened, and its bound is the
        # lookback window and the per-pass limit above. Applying the cap here
        # would be destructive rather than protective: a refused email is
        # stored body-less under a message id that is never offered again, so
        # labelling sixty tickets in one sitting would permanently discard the
        # last ten.
        daily_cap=None,
    )
    if row is None:
        # Stored between the listing and now — another poller, or the same
        # message under a second header we did not collapse.
        stats.duplicate += 1
    elif row.status == "received":
        stats.stored += 1
        # The same pure filter receive_ticket_email stored through, so
        # this counts what is on the row without a second query.
        stats.files += len(_intake.keepable_attachments(message.attachments))
    else:
        stats.refused += 1


def list_inbound_emails(
    conn: psycopg.Connection, user_id: UUID, *, limit: int = 50
) -> list[InboundEmailSummary]:
    """What became of the emails this user forwarded, newest first."""
    return _repository.list_inbound_for_user(conn, user_id, limit)


def run_intake_sweep(
    conn: psycopg.Connection,
    extractor: TicketExtractor,
    *,
    batch_size: int = 20,
    max_emails: int | None = None,
    commit_each: bool = False,
) -> IntakeStats:
    """Read every queued email and act on what the reader says.

    The house sweep shape (the claim sweep documents it): pages over the
    'received' queue, one savepoint per email, a raising email is logged and
    left alone. Three things differ because the reader is a slow, fallible
    network call:

    * Each email's attempt is counted BEFORE it is read, in its own savepoint
      guarded on status, so a reader that always raises runs out of attempts
      (MAX_INTAKE_ATTEMPTS) and the email is marked failed rather than re-read
      for ever — and a row another writer has decided is never read at all.
    * A raising email is excluded from the rest of THIS sweep (`skipped`), so
      one outage cannot burn all its attempts seconds apart; it waits for the
      next interval.
    * A row that already used its attempts but is still 'received' (the
      process died on its last read) is retired here without a read.

    Pages are small: one email is one model call. `max_emails` bounds the
    whole PASS, not the page: without it a first run over a long backlog
    would keep paging until the queue drained, which at seconds of model
    call per email can outlast the scheduler's interval by hours. The queue
    is durable and every status move is guarded, so stopping early costs
    nothing — the next interval picks up where this one stopped.
    """
    stats = IntakeStats()
    skipped: set[UUID] = set()
    while max_emails is None or stats.examined < max_emails:
        page = _repository.list_received_emails(conn, batch_size, excluding=list(skipped))
        if not page:
            break

        progressed = 0
        for email in page:
            if max_emails is not None and stats.examined >= max_emails:
                break
            stats.examined += 1
            if email.attempts >= MAX_INTAKE_ATTEMPTS:
                with conn.transaction():
                    _repository.mark_failed_if_exhausted(
                        conn,
                        email.id,
                        max_attempts=MAX_INTAKE_ATTEMPTS,
                        reason=f"the reader failed {MAX_INTAKE_ATTEMPTS} times",
                    )
                stats.failed += 1
                progressed += 1
                if commit_each:
                    conn.commit()
                continue

            with conn.transaction():
                counted = _repository.bump_attempts(conn, email.id)
            if not counted:
                # Decided by someone else since the page was listed.
                stats.lost_race += 1
                progressed += 1
                continue
            if commit_each:
                # Before the reader, not after: extract() is a network call
                # of seconds, and everything up to here is finished work.
                # Committing now means no transaction is held open across
                # it — no snapshot pinned, no row locked, nothing for an
                # idle-in-transaction timeout to kill.
                conn.commit()

            try:
                reading = _read_email(email, extractor)
                with conn.transaction():
                    status = _apply_reading(conn, email, reading)
                setattr(stats, status, getattr(stats, status) + 1)
                progressed += 1
            except Exception as exc:
                logger.exception(
                    "intake: email %s failed on attempt %d; continuing",
                    email.id,
                    email.attempts + 1,
                )
                stats.errors += 1
                with conn.transaction():
                    exhausted = _repository.mark_failed_if_exhausted(
                        conn,
                        email.id,
                        max_attempts=MAX_INTAKE_ATTEMPTS,
                        reason=(
                            f"the reader failed {MAX_INTAKE_ATTEMPTS} times "
                            f"(last: {type(exc).__name__})"
                        ),
                    )
                if exhausted:
                    stats.failed += 1
                    progressed += 1  # it left the queue, which is progress
                else:
                    skipped.add(email.id)  # not again this sweep
            if commit_each:
                conn.commit()

        if progressed == 0:
            # Defence in depth: `skipped` already keeps a raising page from
            # coming back, so this should not fire. If it does, stop rather
            # than loop.
            logger.error("intake: no email in a page of %d could be decided — stopping", len(page))
            break
    return stats


@dataclass
class _Reading:
    """What the reader said about one email, and what the checks made of it.
    Held apart from the writing so the model call happens with no database
    transaction open."""

    user_id: UUID
    extraction: dict[str, Any]
    verdict: _intake.Verdict


def _read_email(email: InboundEmailRow, extractor: TicketExtractor) -> _Reading:
    """The slow half: hand the email to the reader and judge the answer.
    Touches no database, so the caller can hold no transaction while it
    runs."""
    if email.user_id is None:
        # Unreachable: rows without a user are stored already decided, so the
        # queue never holds one. Said out loud rather than trusted.
        raise RuntimeError(f"queued inbound email {email.id} has no user")
    ticket = extractor.extract(
        subject=email.subject,
        body=_intake.text_for_reading(email.body),
        received_at=email.received_at,
    )
    return _Reading(
        user_id=email.user_id,
        extraction=ticket.model_dump(),
        verdict=_intake.judge(ticket, today=datetime.now(UTC).date()),
    )


def _apply_reading(conn: psycopg.Connection, email: InboundEmailRow, reading: _Reading) -> str:
    """The writing half: act on a reading. Returns the status the email ends
    in — one of IntakeStats's counters.

    Every status write is guarded on the row still being 'received'
    (repository._MARK_PROCESSED), and the write that creates journeys shares
    a savepoint with its status write: if the status was changed by someone
    else meanwhile, the journeys roll back with the savepoint and the answer
    is 'lost_race', never a ticket beside a verdict that disowns it.
    """
    extraction = reading.extraction
    verdict = reading.verdict
    if verdict.plan is None:
        decided = _repository.mark_processed(
            conn, email.id, status=verdict.status, reason=verdict.reason, extraction=extraction
        )
        return verdict.status if decided else "lost_race"

    plan = verdict.plan
    # Per leg, not per ticket: the outbound may have been added by hand
    # before the confirmation was forwarded, and the return still needs
    # tracking. Only legs nobody tracks yet are created; the rest are named.
    new_legs = [
        leg
        for leg in plan.legs
        if not _repository.leg_exists(
            conn,
            reading.user_id,
            leg.travel_date,
            leg.origin_crs,
            leg.destination_crs,
            leg.scheduled_departure,
        )
    ]
    already = len(plan.legs) - len(new_legs)
    if not new_legs:
        decided = _repository.mark_processed(
            conn,
            email.id,
            status="duplicate",
            reason=f"all {already} journey(s) on it were already tracked",
            extraction=extraction,
        )
        return "duplicate" if decided else "lost_race"

    try:
        with conn.transaction():
            journeys = _insert_ticket_and_journeys(
                conn,
                reading.user_id,
                kind=plan.kind,
                price_pence=plan.price_pence,
                source="email",
                legs=new_legs,
                retailer=plan.retailer,
                source_payload=str(email.id),
            )
            reason = f"{len(journeys)} journey(s) added"
            if already:
                reason += f"; {already} already tracked"
            if not _repository.mark_processed(
                conn, email.id, status="parsed", reason=reason, extraction=extraction
            ):
                raise _AlreadyDecided
    except _AlreadyDecided:
        return "lost_race"
    except DuplicateJourney:
        # The leg_exists check and this insert raced another writer for the
        # same journey. Rare; the outcome is still the truth.
        decided = _repository.mark_processed(
            conn,
            email.id,
            status="duplicate",
            reason="these journeys are already tracked",
            extraction=extraction,
        )
        return "duplicate" if decided else "lost_race"
    except InvalidJourney as exc:
        decided = _repository.mark_processed(
            conn,
            email.id,
            status="needs_review",
            reason=f"refused by the database: {exc}",
            extraction=extraction,
        )
        return "needs_review" if decided else "lost_race"
    return "parsed"


# --- Retention --------------------------------------------------------------


def run_retention_sweep(
    conn: psycopg.Connection, *, keep_days: int, batch_size: int = 500, commit_each: bool = False
) -> int:
    """Blank the raw bodies of forwarded emails older than `keep_days`;
    returns how many. A body is kept only as long as a bad read might need
    re-running — the claim window plus a buffer — because it is the most
    personal thing the system holds. The sender, subject, status and the
    reader's structured answer stay, so the user can still see what became
    of what they forwarded. Batched, so a first run over a long backlog
    never holds one long transaction.

    Two passes, because there are two ways a body gets old. The first is an
    email that was read and decided. The second is one that was never read
    at all: with no extractor configured, or one that stayed broken, rows
    keep status 'received' for ever, and a sweep that only looked at decided
    rows would hold their bodies indefinitely — the exact promise this job
    exists to keep. Past the window there is nothing left worth reading
    anyway, the claim window having closed weeks earlier, so those rows are
    retired and blanked together.

    Attachments (0020) go with the body, in a third pass. The count returned
    is bodies only; the files are logged.
    """
    before = datetime.now(UTC) - timedelta(days=keep_days)
    stale = f"never read within {keep_days} days; the claim window had already closed"
    done = 0
    for purge in (
        lambda: _repository.retire_stale_received(conn, before, batch_size, reason=stale),
        lambda: _repository.purge_old_bodies(conn, before, batch_size),
    ):
        while True:
            with conn.transaction():
                batch = purge()
            done += batch
            if commit_each:
                conn.commit()
            if batch < batch_size:
                break
    _purge_orphaned_attachments(conn, batch_size, commit_each=commit_each)
    return done


def _purge_orphaned_attachments(
    conn: psycopg.Connection, batch_size: int, *, commit_each: bool
) -> int:
    """Delete the files hanging off emails whose bodies have been blanked.

    Runs after both body passes so it also covers what this very sweep just
    blanked. Keyed on body_purged_at rather than a date of its own, so the
    files can never outlive the words that say what they are, whatever the
    retention setting is changed to later. Counted separately from the
    bodies and logged, not returned: 'retention blanked 12' meaning some
    mixture of rows and files would be a number nobody could act on.
    """
    deleted = 0
    while True:
        with conn.transaction():
            batch = _repository.purge_attachments_of_purged_bodies(conn, batch_size)
        deleted += batch
        if commit_each:
            conn.commit()
        if batch < batch_size:
            break
    if deleted:
        logger.info("intake retention deleted %d attachments of blanked emails", deleted)
    return deleted


# --- Erasure ----------------------------------------------------------------


def forget_user(conn: psycopg.Connection, user_id: UUID) -> None:
    """The journeys module's part of GDPR erasure (identity.erase_user
    composes the whole). Forwarded emails are deleted outright — bodies are
    the most personal thing the system holds. Tickets lose the seller and
    the reference to the email they came from; the journeys themselves stay,
    because claims reference them and a station pair with a date names no
    one once the user row is anonymised."""
    _repository.delete_inbound_emails(conn, user_id)
    _repository.strip_ticket_sources(conn, user_id)
