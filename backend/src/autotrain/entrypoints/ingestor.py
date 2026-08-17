"""Process type 2 of 4: the ingestor — the rolling delay sweep
(ARCHITECTURE §2).

Polls for journeys past their scheduled arrival, asks the configured arrivals
source what actually happened, and records frozen delay decisions. Everything
downstream (notification worker, claims) only ever reads those decisions.

No real source exists yet — the HSP client is a later PR. Until one is
configured this process refuses to start: sweeping with a source that knows
nothing would age every journey into 'unmatched' for no reason.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

from autotrain.core import db
from autotrain.core.config import get_settings
from autotrain.modules.delays import service

logger = logging.getLogger(__name__)


def _build_source(name: str) -> service.ArrivalsSource:
    # The HSP client will be constructed here once it exists. Adding a source
    # = one branch here + the client module; nothing else changes.
    raise SystemExit(
        f"AUTOTRAIN_ARRIVALS_SOURCE={name!r} has no implementation yet — "
        "the HSP client arrives in a later PR; the ingestor cannot run without a source."
    )


def _sweep_once(source: service.ArrivalsSource) -> service.SweepStats:
    settings = get_settings()
    now = datetime.now(tz=UTC)
    # The lag gives the source time to publish: HSP is next-day data, so a
    # journey is not worth asking about the moment its arrival time passes.
    cutoff = now - timedelta(minutes=settings.ingestor_arrival_lag_minutes)
    give_up_before = now.date() - timedelta(days=settings.ingestor_give_up_days)
    with db.transaction() as conn:
        # commit_each: every journey's decision commits the moment it is
        # made — locks release, finished work survives a crash, and the
        # notification worker sees decisions without waiting for sweep end.
        return service.run_sweep(
            conn,
            source,
            cutoff=cutoff,
            give_up_before=give_up_before,
            batch_size=settings.ingestor_batch_size,
            commit_each=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotrain-ingestor")
    parser.add_argument("--once", action="store_true", help="run one sweep and exit")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    if settings.arrivals_source == "none":
        raise SystemExit(
            "AUTOTRAIN_ARRIVALS_SOURCE=none — nothing to ingest. "
            "Configure a real arrivals source before running the ingestor."
        )
    source = _build_source(settings.arrivals_source)

    db.init_pool()
    try:
        while True:
            try:
                stats = _sweep_once(source)
                logger.info("sweep complete: %s", stats)
            except Exception:
                # A transient failure (database blip mid-sweep) must not kill
                # the process: per-journey commits made completed work
                # durable, so just try again next interval. Broad on purpose
                # — driver exception types are contractually invisible here
                # (.importlinter: psycopg stays behind core).
                logger.exception("sweep failed; retrying next interval")
            if args.once:
                break
            time.sleep(settings.ingestor_interval_seconds)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
