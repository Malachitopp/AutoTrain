"""The notifications module's public API: telling users about their money.

One job today (PLAN §3 item 3): for every qualifying delay detection nobody
has been told about, push "You're owed £6.40 (50%) for your 08:14 to EUS." to
each of the user's registered devices, exactly once.

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
from autotrain.modules.identity.service import PushTarget
from autotrain.modules.journeys import service as _journeys
from autotrain.modules.journeys.service import NotificationContext

logger = logging.getLogger(__name__)

__all__ = [
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


class PushSender(Protocol):
    """Anything that can deliver one push notification to one device.

    Implementations may raise; the sweep isolates the failure to that one
    detection and retries it next pass. Delivery is fire-and-forget — no
    receipt comes back, which is why the stamp, not the send, is the record.
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
    no_target: int = 0  # stamped without a push: no registered device
    errors: int = 0  # send failed and was rolled back; retried next sweep


def run_notification_sweep(
    conn: psycopg.Connection,
    sender: PushSender,
    *,
    batch_size: int = 100,
    commit_each: bool = False,
) -> NotificationStats:
    """Push the "you're owed" message for every unnotified qualifying
    detection, stamping notified_at in the same transaction as each send.

    The page fetch locks its rows (delays.list_unnotified_detections), so a
    concurrent worker skips them rather than double-sending. commit_each
    commits at each PAGE boundary — not per detection, because the row locks
    are what hold the exactly-once guarantee together and they only release
    at commit. A crash therefore re-delivers at most one page; batch_size is
    the duplicate ceiling as much as it is a memory bound.

    A detection whose user has no registered device is stamped and counted in
    no_target: tokens arrive when the user installs the app, and week-old news
    must not greet them as a push storm. The absent push is the record, the
    same shape as the claim sweep's no_operator rows.
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
        contexts = _journeys.notification_contexts(conn, [d.journey_id for d in fresh])
        targets = _identity.push_targets(conn, [c.user_id for c in contexts.values()])

        for detection in fresh:
            stats.examined += 1
            try:
                with conn.transaction():
                    _notify_one(
                        conn, sender, detection, contexts.get(detection.journey_id), targets, stats
                    )
            except Exception:
                logger.exception(
                    "notification sweep: detection %s failed; continuing", detection.id
                )
                stats.errors += 1
                failed.add(detection.id)
        if commit_each:
            conn.commit()
        if len(page) < limit:
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
    sender: PushSender,
    detection: PendingNotification,
    context: NotificationContext | None,
    targets: dict[UUID, list[PushTarget]],
    stats: NotificationStats,
) -> None:
    if context is None:
        # Effectively unreachable: delay_detections cascades from journeys,
        # so a deleted journey takes its detection with it. Stamp anyway so a
        # surprise never wedges the queue head.
        logger.info("notification sweep: detection %s has no journey", detection.id)
        stats.no_target += 1
        _delays.mark_notified(conn, detection.id)
        return

    devices = targets.get(context.user_id, [])
    if not devices:
        # No registered device — stamped so the queue drains, never re-raised
        # when a device appears later: stale news is not a welcome push.
        stats.no_target += 1
        _delays.mark_notified(conn, detection.id)
        return

    body = _body(context, detection)
    for device in devices:
        sender.send(token=device.push_token, platform=device.platform, title=_TITLE, body=body)
        stats.pushes += 1
    _delays.mark_notified(conn, detection.id)
    stats.notified += 1


def _body(context: NotificationContext, detection: PendingNotification) -> str:
    """PLAN §3's exact sentence: "You're owed £6.40 (50%) for your 08:14 to
    Euston." — with the destination as a CRS code until a stations reference
    table exists. Integer pence throughout (§9): no float ever touches money.
    """
    pence = detection.entitlement_pence
    money = f"£{pence // 100}.{pence % 100:02d}"
    band = f" ({detection.band_percent}%)" if detection.band_percent is not None else ""
    departs = context.scheduled_departure.astimezone(_LONDON).strftime("%H:%M")
    return f"You're owed {money}{band} for your {departs} to {context.destination_crs}."
