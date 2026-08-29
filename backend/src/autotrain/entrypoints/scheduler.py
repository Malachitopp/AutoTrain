"""Process type 4 of 4: the scheduler — recurring jobs on an interval
(ARCHITECTURE §2; the enumeration is api, ingestor, worker, scheduler).

Two jobs today, both from the claims module, both cheap no-ops when their
work queues are empty:

  * claim sweep  — opens a draft claim for every entitled delay detection
    that does not have one (the delay_detections_unclaimed_idx queue, 0010);
  * claim expiry — expires claims whose filing window closed, so a claim we
    never managed to file ends with an audited answer, not silence.

Jobs are isolated from each other: one failing is logged and retried next
interval while the rest still run — the same containment stance as the
ingestor's sweep loop, applied per job. Both jobs are idempotent (the
one-claim-per-journey constraint and the guarded expiry transition), so a
crash between interval N and N+1 needs no recovery logic; the next interval
simply does whatever remains.

Unlike the ingestor this process needs no external source and no credentials,
so there is nothing to validate at boot beyond Settings itself.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from autotrain.core import db
from autotrain.core.config import get_settings
from autotrain.modules.claims import service as claims

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


def _claim_sweep_once(batch_size: int) -> claims.ClaimSweepStats:
    with db.transaction() as conn:
        # commit_each for the same reasons as the ingestor: every claim
        # commits the moment it exists, so finished work survives a
        # mid-sweep crash and is visible without waiting for sweep end.
        return claims.run_claim_sweep(conn, batch_size=batch_size, commit_each=True)


def _expire_once(batch_size: int) -> int:
    today = _uk_today(datetime.now(tz=UTC))
    with db.transaction() as conn:
        return claims.expire_overdue(conn, today, batch_size=batch_size, commit_each=True)


def _run_jobs_once(batch_size: int) -> None:
    """One pass over every job, each isolated: a job that raises is logged
    and retried next interval; the jobs after it still run. Broad excepts on
    purpose — driver exception types are contractually invisible here
    (.importlinter: psycopg stays behind core)."""
    try:
        stats = _claim_sweep_once(batch_size)
        logger.info("claim sweep complete: %s", stats)
    except Exception:
        logger.exception("claim sweep failed; retrying next interval")
    try:
        expired = _expire_once(batch_size)
        logger.info("claim expiry complete: expired=%d", expired)
    except Exception:
        logger.exception("claim expiry failed; retrying next interval")


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotrain-scheduler")
    parser.add_argument("--once", action="store_true", help="run every job once and exit")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    db.init_pool()
    try:
        while True:
            _run_jobs_once(settings.scheduler_batch_size)
            if args.once:
                break
            time.sleep(settings.scheduler_interval_seconds)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
