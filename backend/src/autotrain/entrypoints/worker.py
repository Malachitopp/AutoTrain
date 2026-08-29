"""Process type 3 of 4: the worker — queue consumers (ARCHITECTURE §2).

One queue today: unnotified qualifying delay detections
(delay_detections_pending_idx, 0006), drained by the notification sweep. The
queue is the database itself — polled on an interval, rows claimed with FOR
UPDATE SKIP LOCKED — which is exactly the §2 shape minus SQS: when queues
arrive, the poll becomes a consume and the sweep's locking discipline is
already correct for multiple workers. Claim submission (v2 filing) gets its
own queue and joins this process later (§6).

Without a configured push sender this process refuses to start — a worker
that silently stamps detections as notified while delivering nothing would
be worse than no worker at all.
"""

from __future__ import annotations

import argparse
import logging
import time

from autotrain.core import db
from autotrain.core.config import Settings, get_settings
from autotrain.modules.notifications import service as notifications
from autotrain.sources.push import LogPushSender

logger = logging.getLogger(__name__)


def _build_sender(settings: Settings) -> notifications.PushSender:
    # One branch per sender; adding one (FCM, APNs) touches this function and
    # the sources/ module, nothing else — the ingestor's _build_source shape.
    if settings.push_sender == "log":
        return LogPushSender()
    raise SystemExit(
        "AUTOTRAIN_PUSH_SENDER=none — nowhere to deliver. Configure a push "
        "sender (log) before running the worker."
    )


def _sweep_once(sender: notifications.PushSender) -> notifications.NotificationStats:
    settings = get_settings()
    with db.transaction() as conn:
        # commit_each: each PAGE of sends commits as it finishes — stamps
        # become durable and row locks release, so a crash re-delivers at
        # most one page (the sweep's own docstring owns that argument).
        return notifications.run_notification_sweep(
            conn, sender, batch_size=settings.worker_batch_size, commit_each=True
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotrain-worker")
    parser.add_argument("--once", action="store_true", help="run one sweep and exit")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    sender = _build_sender(settings)

    db.init_pool()
    try:
        while True:
            try:
                stats = _sweep_once(sender)
                logger.info("notification sweep complete: %s", stats)
            except Exception:
                # Broad on purpose — driver exception types are contractually
                # invisible here (.importlinter: psycopg stays behind core).
                logger.exception("notification sweep failed; retrying next interval")
            if args.once:
                break
            time.sleep(settings.worker_interval_seconds)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
