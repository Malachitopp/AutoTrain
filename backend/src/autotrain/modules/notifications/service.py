"""The notifications module's public API: telling users about their money.

One job today (PLAN §3 item 3): for every qualifying delay detection nobody
has been told about, deliver "You're owed £6.40 (50%) for your 08:14 to EUS."
exactly once — as a push to each of the user's registered devices, or, for a
user who has none, as an email to the address on their account.

Email is not a lesser fallback here; it is what the news travels by until
there is an app to push to, and for a deployment that will never have one it
is the whole channel. Both transports are optional and both are protocols
implemented outside the module.

No repository, deliberately: notifications owns no tables. Its work queue is
`delay_detections.notified_at` (0006 guarantee 3, delays-owned, driven through
delays.service), the journey facts in the message come through
journeys.service, and device tokens come through identity.service. If this
module ever owns state — a notification log, per-user preferences — a
repository appears with the migration that creates it.

The delivery transport is the PushSender protocol below, implemented outside
the module (sources/push.py, following delays' ArrivalsSource precedent) and
injected by the worker entrypoint — the module never marries a transport.

Exactly-once, precisely: the queue rows come back locked (FOR UPDATE SKIP
LOCKED) and the notified_at stamp commits in the same transaction as the
send, so two workers cannot double-send and a crash before commit means
retry, not silence. The unavoidable residue is at-least-once at the edges: a
crash after the push left the process but before COMMIT re-sends on the next
sweep, and a multi-device user whose second send fails will see the first
device pushed again on retry. Duplicates are annoying; silence is money lost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg

# Private aliases for the same reason as in the other module services.
from autotrain.modules.delays import service as _delays

# Re-exported: the queue row this sweep consumes, so a caller (or test fake)
# needs exactly one import.
from autotrain.modules.delays.service import PendingNotification
from autotrain.modules.identity import service as _identity

# EmailSender is identity's protocol, imported rather than redeclared: there
# is one answer in this system to "what can send an email", and two identical
# Protocols would drift. Nothing about it is identity-specific — it is the
# same transport the magic-link login uses, built in sources/email.py and
# handed in by the entrypoint.
from autotrain.modules.identity.service import EmailSender, PushTarget
from autotrain.modules.journeys import service as _journeys
from autotrain.modules.journeys.service import NotificationContext

logger = logging.getLogger(__name__)

__all__ = [
    "EmailSender",
    "NotificationStats",
    "PendingNotification",
    "PushSender",
    "run_notification_sweep",
]

# Departure times render in UK wall-clock time: the user boarded "the 08:14",
# not "the 07:14 UTC" (ARCHITECTURE §9: UTC in the database, Europe/London at
# render — and a push notification is a render).
_LONDON = ZoneInfo("Europe/London")

_TITLE = "Delay Repay"

# A sweep gives up after this many distinct send failures: past a handful the
# provider is down, not the rows. It bounds the widened fetch below (limit
# never exceeds batch_size + this), and turns an outage into "attempt a few,
# defer the backlog to the next interval" instead of a sweep that re-locks and
# re-sorts every failed row again on every page for as long as the queue lasts.
_MAX_FAILURES_PER_SWEEP = 25


class PushSender(Protocol):
    """Anything that can deliver one push notification to one device.

    Implementations may raise; the sweep isolates the failure to that one
    detection and retries it next pass. They MUST bound their own delivery
    time (connect/read timeouts): the sweep holds row locks on the whole page
    while send() runs, and a hung send would hold them indefinitely — locks
    the claims sweep's stamp on the same rows queues behind. Delivery is
    fire-and-forget — no receipt comes back, which is why the stamp, not the
    send, is the record.
    """

    def send(self, *, token: str, platform: str, title: str, body: str) -> None: ...


@dataclass
class NotificationStats:
    """One notification sweep's outcome, for the worker's log line."""

    # Examinations, not distinct detections: a detection whose send fails
    # stays in the queue and may be seen again by a later sweep.
    examined: int = 0
    notified: int = 0  # detections stamped after at least one successful push
    pushes: int = 0  # device deliveries (a user may have several devices)
    emails: int = 0  # detections emailed instead, having no device
    no_target: int = 0  # stamped with nothing sent: no device and no address
    errors: int = 0  # send failed and was rolled back; retried next sweep


def run_notification_sweep(
    conn: psycopg.Connection,
    sender: PushSender | None = None,
    *,
    email_sender: EmailSender | None = None,
    app_base_url: str | None = None,
    batch_size: int = 100,
    commit_each: bool = False,
) -> NotificationStats:
    """Tell the user about every unnotified qualifying detection, stamping
    notified_at in the same transaction as each send.

    Two channels, tried in that order, and both optional — the caller
    configures what exists and this decides per user:

    * a push to each registered device, when there is a push sender and the
      user has devices;
    * failing that, an email to the account's own address, when there is an
      email sender.

    The order is not a preference so much as a fact about what a device
    means: a registered device is a person who installed something and
    asked to be interrupted, which is a better channel than email whenever
    it exists. Email is what makes the system useful before it does — and
    for a deployment with no app at all, it is the only channel there is.
    Neither configured is a valid, if useless, sweep: everything is stamped
    as no_target, which is exactly what happens today for a user with no
    devices.

    The page fetch locks its rows (delays.list_unnotified_detections), so a
    concurrent worker skips them rather than double-sending. commit_each
    commits at each PAGE boundary — not per detection, because the row locks
    are what hold the exactly-once guarantee together and they only release
    at commit. A crash therefore re-delivers at most one page; batch_size is
    the duplicate ceiling as much as it is a memory bound. The flip side:
    the locks are held WHILE sending, so batch_size also bounds how long a
    concurrent writer to the same rows (the claims sweep's stamp) can queue
    behind a page of real network sends — keep it modest, and see the
    PushSender timeout requirement.

    A detection that reached neither channel is stamped anyway and counted
    in no_target: tokens arrive when the user installs the app, and week-old
    news must not greet them as a push storm. The absent delivery is the
    record, the same shape as the claim sweep's no_operator rows.
    """
    stats = NotificationStats()
    # Send failures this run, never retried within it: a failed row stays in
    # the queue (its savepoint rolled back), so re-encountering it would mean
    # re-sending — and a partially-delivered multi-device user would be pushed
    # again inside one sweep. Failed rows also clog the queue head, which is
    # why the fetch below widens by len(failed): the page must always be able
    # to see PAST them to fresh work, or one poisoned row at the head would
    # starve everything behind it. Termination: every fresh row ends stamped
    # or failed, so `fresh` strictly shrinks toward empty.
    failed: set[UUID] = set()
    while True:
        limit = batch_size + len(failed)
        page = _delays.list_unnotified_detections(conn, limit)
        fresh = [d for d in page if d.id not in failed]
        if not fresh:
            break
        # However wide the fetch, at most batch_size sends share one commit —
        # that keeps the crash-duplicate ceiling at one page even when another
        # worker has stamped our failures and the widened page is all fresh.
        overflow = len(fresh) > batch_size
        fresh = fresh[:batch_size]
        contexts = _journeys.notification_contexts(conn, [d.journey_id for d in fresh])
        targets = _identity.push_targets(conn, [c.user_id for c in contexts.values()])

        for detection in fresh:
            stats.examined += 1
            try:
                with conn.transaction():
                    _notify_one(
                        conn,
                        detection,
                        contexts.get(detection.journey_id),
                        targets,
                        stats,
                        sender=sender,
                        email_sender=email_sender,
                        app_base_url=app_base_url,
                    )
            except Exception:
                logger.exception(
                    "notification sweep: detection %s failed; continuing", detection.id
                )
                stats.errors += 1
                failed.add(detection.id)
        if commit_each:
            conn.commit()
        if len(failed) >= _MAX_FAILURES_PER_SWEEP:
            logger.warning(
                "notification sweep: %d send failures — provider trouble, deferring the "
                "rest of the queue to the next pass",
                len(failed),
            )
            break
        if len(page) < limit and not overflow:
            # This page reached the end of the queue, and everything in it is
            # now stamped or failed — the next fetch could only repeat failures.
            break
    if failed:
        logger.warning(
            "notification sweep: %d detections failed and remain queued for the next pass",
            len(failed),
        )
    return stats


def _notify_one(
    conn: psycopg.Connection,
    detection: PendingNotification,
    context: NotificationContext | None,
    targets: dict[UUID, list[PushTarget]],
    stats: NotificationStats,
    *,
    sender: PushSender | None,
    email_sender: EmailSender | None,
    app_base_url: str | None,
) -> None:
    if context is None:
        # Effectively unreachable: delay_detections cascades from journeys,
        # so a deleted journey takes its detection with it. Stamp anyway so a
        # surprise never wedges the queue head.
        logger.info("notification sweep: detection %s has no journey", detection.id)
        stats.no_target += 1
        _delays.mark_notified(conn, detection.id)
        return

    devices = targets.get(context.user_id, []) if sender is not None else []
    delivered = 0
    if sender is not None and devices:
        body = _body(context, detection)
        for device in devices:
            sender.send(token=device.push_token, platform=device.platform, title=_TITLE, body=body)
            stats.pushes += 1
            delivered += 1
    elif email_sender is not None:
        # Only asked for when it is about to be used: an address is the
        # personal data this system works hardest not to move around, and a
        # sweep over a page of detections should not load one per row it is
        # going to push to a phone instead.
        address = _identity.account_email(conn, context.user_id)
        if address:
            email_sender.send_email(
                to=address,
                subject=_email_subject(context, detection),
                body=_email_body(context, detection, app_base_url=app_base_url),
            )
            stats.emails += 1
            delivered += 1

    if delivered:
        stats.notified += 1
    else:
        # No device and no address, or no transport configured at all.
        # Stamped rather than retried: tokens arrive when the app is
        # installed, and week-old news must not greet a new device as a
        # push storm. The absent delivery is the record, the same shape as
        # the claim sweep's no_operator rows.
        stats.no_target += 1
    _delays.mark_notified(conn, detection.id)


def _body(context: NotificationContext, detection: PendingNotification) -> str:
    """PLAN §3's exact sentence: "You're owed £6.40 (50%) for your 08:14 to
    Euston." — with the destination as a CRS code until a stations reference
    table exists. Integer pence throughout (§9): no float ever touches money.
    """
    money = _money(detection.entitlement_pence)
    band = f" ({detection.band_percent}%)" if detection.band_percent is not None else ""
    return f"You're owed {money}{band} for your {_departs(context)} to {context.destination_crs}."


def _email_subject(context: NotificationContext, detection: PendingNotification) -> str:
    """What the inbox list shows. The money and the train, in that order,
    because a subject line is read from the left and truncated from the
    right — "You're owed £6.40 for your 08:14 to EUS"."""
    return (
        f"You're owed {_money(detection.entitlement_pence)} "
        f"for your {_departs(context)} to {context.destination_crs}"
    )


def _email_body(
    context: NotificationContext,
    detection: PendingNotification,
    *,
    app_base_url: str | None,
) -> str:
    """The same news as the push, with the detail an email has room for.

    A push interrupts and has to be one sentence. An email is read later,
    possibly weeks later, and by then "your 08:14 to EUS" may not be enough
    to tell which journey it was — so the date and the route are spelled
    out. Delay Repay closes 28 days after travel, and the travel date is
    what says how much of that is left.
    """
    departure = context.scheduled_departure.astimezone(_LONDON)
    lines = [
        _body(context, detection),
        "",
        f"Journey   {context.origin_crs} to {context.destination_crs}",
        f"Date      {departure.strftime('%A %d %B %Y')}",
        f"Departed  {departure.strftime('%H:%M')}",
        "",
        "AutoTrain has opened a claim for this journey. Delay Repay closes",
        "28 days after the date above.",
    ]
    if app_base_url:
        lines += ["", f"Open AutoTrain to check and file it: {app_base_url}"]
    lines.append("")
    return "\n".join(lines)


def _money(pence: int) -> str:
    """Integer pence as pounds (§9): no float ever touches money."""
    return f"£{pence // 100}.{pence % 100:02d}"


def _departs(context: NotificationContext) -> str:
    """The departure as UK wall-clock time — "the 08:14", as the user
    boarded it (ARCHITECTURE §9: UTC stored, Europe/London rendered)."""
    return context.scheduled_departure.astimezone(_LONDON).strftime("%H:%M")
