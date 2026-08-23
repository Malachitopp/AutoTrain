"""Process type 2 of 4: the ingestor — the rolling delay sweep
(ARCHITECTURE §2).

Polls for journeys past their scheduled arrival, asks the configured arrivals
source what actually happened, and records frozen delay decisions. Everything
downstream (notification worker, claims) only ever reads those decisions.

The one real source today is HSP (sources/hsp.py). Without a configured
source this process refuses to start — sweeping with a source that knows
nothing would age journeys toward 'unmatched' for no reason — and credential
presence is enforced by Settings itself, so a misconfigured container dies
at boot with a validation error, not mid-sweep with per-journey 401s.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from autotrain.core import db
from autotrain.core.config import Settings, get_settings
from autotrain.modules.delays import service
from autotrain.sources.hsp import HspSource

logger = logging.getLogger(__name__)

_LONDON = ZoneInfo("Europe/London")


def _build_source(settings: Settings) -> service.ArrivalsSource:
    # One branch per source; adding one touches this function and the
    # sources/ module, nothing else. Credential PRESENCE is validated by
    # Settings — the check here only narrows the Optional types.
    if settings.arrivals_source == "hsp":
        if not settings.hsp_email or settings.hsp_password is None:
            raise SystemExit(
                "hsp credentials missing — Settings validation should have caught this"
            )
        return HspSource.from_credentials(
            settings.hsp_email, settings.hsp_password.get_secret_value()
        )
    raise SystemExit(
        "AUTOTRAIN_ARRIVALS_SOURCE=none — nothing to ingest. Configure a real "
        "arrivals source (hsp) before running the ingestor."
    )


def _sweep_once(source: service.ArrivalsSource) -> service.SweepStats:
    settings = get_settings()
    now = datetime.now(tz=UTC)
    # The lag gives the source time to publish: HSP is next-day data, so a
    # journey is not worth asking about the moment its arrival time passes.
    cutoff = now - timedelta(minutes=settings.ingestor_arrival_lag_minutes)
    # travel_date is a UK timetable day, so the give-up boundary must come
    # from the UK calendar date — the UTC date lags it by an hour a night
    # during BST.
    give_up_before = now.astimezone(_LONDON).date() - timedelta(days=settings.ingestor_give_up_days)
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

    source = _build_source(settings)

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
        # The protocol doesn't demand a close(); sources that hold network
        # clients (HSP) provide one.
        close = getattr(source, "close", None)
        if close is not None:
            close()
        db.close_pool()


if __name__ == "__main__":
    main()
