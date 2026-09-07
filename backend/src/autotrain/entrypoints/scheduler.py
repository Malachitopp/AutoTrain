"""Process type 4 of 4: the scheduler — recurring jobs on an interval
(ARCHITECTURE §2; the enumeration is api, ingestor, worker, scheduler).

Four jobs today, all cheap no-ops when their work queues are empty:

  * claim sweep  — opens a draft claim for every entitled delay detection
    that does not have one (the delay_detections_unclaimed_idx queue, 0010);
  * claim expiry — expires claims whose filing window closed, so a claim we
    never managed to file ends with an audited answer, not silence;
  * ticket intake — reads forwarded ticket emails into journeys
    (inbound_emails_queue_idx, 0013). Runs only when a reader is configured
    (AUTOTRAIN_TICKET_EXTRACTOR); the reader is built here, once, and handed
    to the journeys module — the sources/ seam, as in the ingestor;
  * intake retention — blanks the raw bodies of emails decided long enough
    ago (inbound_emails_retention_idx, 0015).

Jobs are isolated from each other: one failing is logged and retried next
interval while the rest still run — the same containment stance as the
ingestor's sweep loop, applied per job. Every job is idempotent (guarded
transitions, ON CONFLICT inserts, status-guarded marks), so a crash between
interval N and N+1 needs no recovery logic; the next interval simply does
whatever remains.

Each job runs under its own advisory lock (core.db.job_lock): a second
scheduler — a rolling deploy's overlap, or a scale-out — finds the lock held
and skips that job's pass, instead of doing the same work, and paying for
the same model calls, twice.

The claims jobs need no external source and no credentials; the intake job
is the exception, and Settings refuses to boot with a reader and no key.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from autotrain.core import db
from autotrain.core.config import get_settings
from autotrain.core.observability import fields, setup_logging
from autotrain.modules.claims import service as claims
from autotrain.modules.journeys import service as journeys
from autotrain.sources.ticket_emails import ClaudeTicketExtractor

logger = logging.getLogger(__name__)

_LONDON = ZoneInfo("Europe/London")


def _uk_today(now: datetime) -> date:
    """The UK calendar date for `now` — the expiry boundary.

    file_by is derived from travel_date, a UK timetable day, so "has the
    filing window closed" must be judged against the UK calendar date. The
    UTC date lags it by an hour a night during BST (the same hour as the
    ingestor's give-up boundary): judged by it, a claim would still look
    filable for an hour after its last valid UK day had ended — the
    deadline must move exactly at UK midnight, not an hour later.
    """
    return now.astimezone(_LONDON).date()


# Each job below returns None when another scheduler holds its lock: the
# pass is skipped, not failed, and the next interval tries again.


def _claim_sweep_once(batch_size: int) -> claims.ClaimSweepStats | None:
    with db.transaction() as conn, db.job_lock(conn, "claim-sweep") as held:
        if not held:
            return None
        # commit_each for the same reasons as the ingestor: every claim
        # commits the moment it exists, so finished work survives a
        # mid-sweep crash and is visible without waiting for sweep end.
        return claims.run_claim_sweep(conn, batch_size=batch_size, commit_each=True)


def _expire_once(batch_size: int) -> int | None:
    today = _uk_today(datetime.now(tz=UTC))
    with db.transaction() as conn, db.job_lock(conn, "claim-expiry") as held:
        if not held:
            return None
        return claims.expire_overdue(conn, today, batch_size=batch_size, commit_each=True)


def _build_extractor() -> journeys.TicketExtractor | None:
    """The configured ticket reader, or None when intake is switched off.
    One branch per reader; adding one touches this function and sources/."""
    settings = get_settings()
    if settings.ticket_extractor == "claude":
        if settings.anthropic_api_key is None:  # unreachable: Settings validates the pair
            raise SystemExit("AUTOTRAIN_TICKET_EXTRACTOR=claude needs AUTOTRAIN_ANTHROPIC_API_KEY")
        return ClaudeTicketExtractor(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.ticket_extractor_model,
        )
    return None


def _intake_once(extractor: journeys.TicketExtractor) -> journeys.IntakeStats | None:
    settings = get_settings()
    with db.transaction() as conn, db.job_lock(conn, "ticket-intake") as held:
        if not held:
            return None
        # commit_each: every email's outcome is durable the moment it is
        # decided — a model call is seconds, and a crash mid-pass must not
        # undo the emails already read.
        return journeys.run_intake_sweep(
            conn,
            extractor,
            batch_size=settings.intake_batch_size,
            max_emails=settings.intake_max_per_pass,
            commit_each=True,
        )


def _retention_once() -> int | None:
    settings = get_settings()
    with db.transaction() as conn, db.job_lock(conn, "intake-retention") as held:
        if not held:
            return None
        return journeys.run_retention_sweep(
            conn, keep_days=settings.intake_body_retention_days, commit_each=True
        )


def _as_fields(result: Any) -> dict[str, Any]:
    """A job's result as log fields: a stats dataclass field by field, a
    bare count under 'count'."""
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return fields(result)
    return {"count": result}


def _run_jobs_once(batch_size: int, extractor: journeys.TicketExtractor | None = None) -> None:
    """One pass over every job, each isolated: a job that raises is logged
    and retried next interval; the jobs after it still run. Broad excepts on
    purpose — driver exception types are contractually invisible here
    (.importlinter: psycopg stays behind core)."""
    jobs: list[tuple[str, Callable[[], Any]]] = [
        ("claim sweep", lambda: _claim_sweep_once(batch_size)),
        ("claim expiry", lambda: _expire_once(batch_size)),
    ]
    if extractor is not None:
        reader = extractor
        jobs.append(("ticket intake", lambda: _intake_once(reader)))
    jobs.append(("intake retention", _retention_once))
    for name, run in jobs:
        try:
            result = run()
        except Exception:
            logger.exception("%s failed; retrying next interval", name)
            continue
        if result is None:
            logger.info("%s skipped: another scheduler holds its lock", name)
        else:
            logger.info("%s complete", name, extra=_as_fields(result))


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotrain-scheduler")
    parser.add_argument("--once", action="store_true", help="run every job once and exit")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging("scheduler", level=settings.log_level, fmt=settings.log_format)

    extractor = _build_extractor()
    if extractor is None:
        logger.info("ticket intake off: AUTOTRAIN_TICKET_EXTRACTOR=none")

    db.init_pool()
    try:
        while True:
            _run_jobs_once(settings.scheduler_batch_size, extractor)
            if args.once:
                break
            time.sleep(settings.scheduler_interval_seconds)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
