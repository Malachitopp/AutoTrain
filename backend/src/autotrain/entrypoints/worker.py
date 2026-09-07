"""Process type 3 of 4: the worker — queue consumers (ARCHITECTURE §2).

One queue today: unnotified qualifying delay detections
(delay_detections_pending_idx, 0006), drained by the notification sweep. The
queue is the database itself — polled on an interval, rows claimed with FOR
UPDATE SKIP LOCKED — which is exactly the §2 shape minus SQS: when queues
arrive, the poll becomes a consume and the sweep's locking discipline is
already correct for multiple workers. Claim submission (v2 filing) gets its
own queue and joins this process later (§6).

Two transports, either or both: a push to registered devices, and an email
to the account's own address for users who have no device. With NEITHER
configured this process refuses to start — a worker that silently stamps
detections as notified while delivering nothing would be worse than no
worker at all. Email alone is a complete configuration, and is the one a
deployment with no mobile app runs.
"""

from __future__ import annotations

import argparse
import logging
import time

from autotrain.core import db
from autotrain.core.config import Settings, get_settings
from autotrain.core.observability import fields, setup_logging
from autotrain.modules.notifications import service as notifications
from autotrain.sources.email import LogEmailSender, ResendEmailSender
from autotrain.sources.push import LogPushSender

logger = logging.getLogger(__name__)


def _build_push_sender(settings: Settings) -> notifications.PushSender | None:
    # One branch per sender; adding one (FCM, APNs) touches this function and
    # the sources/ module, nothing else — the ingestor's _build_source shape.
    if settings.push_sender == "log":
        return LogPushSender()
    return None


def _build_email_sender(settings: Settings) -> notifications.EmailSender | None:
    """The transport for users with no registered device — the same one the
    magic-link login uses, built the same way (routers/auth.py).

    Held for the life of the process rather than built per sweep: the Resend
    sender owns an HTTP client, and one built per use re-handshakes TLS
    every time.
    """
    if settings.email_sender == "log":
        return LogEmailSender()
    if settings.email_sender == "resend":
        if settings.resend_api_key is None or not settings.email_from:
            # Unreachable: Settings refuses to boot with 'resend' and no
            # credentials. Kept because the type checker cannot see that.
            raise SystemExit("AUTOTRAIN_EMAIL_SENDER=resend is missing its credentials")
        return ResendEmailSender.from_api_key(
            settings.resend_api_key.get_secret_value(), from_address=settings.email_from
        )
    return None


def _build_channels(
    settings: Settings,
) -> tuple[notifications.PushSender | None, notifications.EmailSender | None]:
    """Every transport this deployment has, and the refusal when it has none.

    Either channel alone is a complete configuration — email alone is what a
    deployment with no mobile app runs, and push alone is what one with an
    app and no mail provider runs. Neither is refused here rather than
    discovered later as a queue that drains into nothing: the sweep stamps
    every detection it examines, so a worker with no transport would quietly
    mark a month of real money as told-about.
    """
    push = _build_push_sender(settings)
    email = _build_email_sender(settings)
    if push is None and email is None:
        raise SystemExit(
            "AUTOTRAIN_PUSH_SENDER=none and AUTOTRAIN_EMAIL_SENDER=none — nowhere "
            "to deliver. Configure a push sender (log) or an email sender "
            "(log, resend) before running the worker."
        )
    return push, email


def _sweep_once(
    sender: notifications.PushSender | None = None,
    *,
    email_sender: notifications.EmailSender | None = None,
) -> notifications.NotificationStats:
    settings = get_settings()
    with db.transaction() as conn:
        # commit_each: each PAGE of sends commits as it finishes — stamps
        # become durable and row locks release, so a crash re-delivers at
        # most one page (the sweep's own docstring owns that argument).
        return notifications.run_notification_sweep(
            conn,
            sender,
            email_sender=email_sender,
            app_base_url=settings.app_base_url,
            batch_size=settings.worker_batch_size,
            commit_each=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotrain-worker")
    parser.add_argument("--once", action="store_true", help="run one sweep and exit")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging("worker", level=settings.log_level, fmt=settings.log_format)

    sender, email_sender = _build_channels(settings)

    db.init_pool()
    try:
        while True:
            try:
                stats = _sweep_once(sender, email_sender=email_sender)
                logger.info("notification sweep complete", extra=fields(stats))
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
