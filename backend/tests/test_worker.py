"""Worker tests: sender selection, the end-to-end sweep through the
entrypoint, and the FOR UPDATE SKIP LOCKED guarantee that two workers cannot
pick up the same detection — the one property that needs two real
connections, which the rollback `conn` fixture cannot provide.

Committed-world scaffolding as in test_scheduler: these tests commit, so they
clean up their rows in FK order even when they fail."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest

from autotrain.core import db
from autotrain.core.config import get_settings
from autotrain.entrypoints import worker
from autotrain.modules.delays import service as delays
from autotrain.modules.notifications import service as notifications
from autotrain.sources.push import LogPushSender
from conftest import TEST_DATABASE_URL
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

_DEP = datetime(2026, 6, 15, 7, 14, tzinfo=UTC)  # 08:14 BST
_TOKEN = "worker-test-token-abcdef123456"


@contextmanager
def _committed_world() -> Iterator[tuple[psycopg.Connection, UUID, UUID]]:
    """(setup_conn, user_id, detection_id): one committed user with a device
    and one unnotified qualifying detection — the worker's whole world.
    Deleted again in FK order (journeys cascades detections; users cascades
    devices) even when the test body fails."""
    setup = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    user_id = None
    try:
        user_id = _mk_user(setup, "worker-test@example.com")
        setup.execute(
            "INSERT INTO devices (user_id, platform, push_token) VALUES (%s, 'ios', %s)",
            (user_id, _TOKEN),
        )
        ticket_id = _scalar(
            setup.execute(
                "INSERT INTO tickets (user_id, kind, price_pence, source) "
                "VALUES (%s, 'single', 1280, 'manual') RETURNING id",
                (user_id,),
            )
        )
        journey_id = _scalar(
            setup.execute(
                "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
                "travel_date, scheduled_departure, scheduled_arrival, status) "
                "VALUES (%s, %s, 'MAN', 'EUS', %s, %s, %s, 'assessed') RETURNING id",
                (user_id, ticket_id, _DEP.date(), _DEP, _DEP + timedelta(hours=2)),
            )
        )
        detection_id = _scalar(
            setup.execute(
                "INSERT INTO delay_detections (journey_id, actual_arrival, delay_minutes, "
                "source, band_percent, entitlement_pence) "
                "VALUES (%s, %s, 45, 'hsp', 50, 640) RETURNING id",
                (journey_id, _DEP + timedelta(hours=2, minutes=45)),
            )
        )
        yield setup, user_id, detection_id
    finally:
        if user_id is not None:
            setup.execute("DELETE FROM journeys WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM tickets WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM users WHERE id = %s", (user_id,))
        setup.close()


# --- Sender selection ---------------------------------------------------------


def test_build_sender_refuses_to_run_without_one() -> None:
    """A worker that stamps detections while delivering nothing would be
    worse than no worker: the refusal is the safety feature."""
    settings = get_settings().model_copy(update={"push_sender": "none"})
    with pytest.raises(SystemExit):
        worker._build_sender(settings)


def test_build_sender_log() -> None:
    settings = get_settings().model_copy(update={"push_sender": "log"})
    assert isinstance(worker._build_sender(settings), LogPushSender)


# --- Two workers, one queue ---------------------------------------------------


@pytest.mark.usefixtures("migrated_database")
def test_skip_locked_hides_in_flight_rows_from_a_second_worker() -> None:
    """0006 guarantee 3's mechanism, proved with two real connections: while
    worker A holds a detection's row lock, worker B's queue read glides past
    it — and the row reappears the moment A lets go."""
    with _committed_world() as (_, _, detection_id):
        with (
            psycopg.connect(TEST_DATABASE_URL) as worker_a,
            psycopg.connect(TEST_DATABASE_URL) as worker_b,
        ):
            locked = delays.list_unnotified_detections(worker_a, 50)
            assert detection_id in {d.id for d in locked}

            # B sees nothing A holds — no blocking, no double-send.
            unlocked_view = delays.list_unnotified_detections(worker_b, 50)
            assert detection_id not in {d.id for d in unlocked_view}

            # A gives up (crash, rollback): the work is B's to take.
            worker_a.rollback()
            retry_view = delays.list_unnotified_detections(worker_b, 50)
            assert detection_id in {d.id for d in retry_view}
            worker_b.rollback()


@pytest.mark.usefixtures("pool")
def test_page_boundary_commits_survive_multiple_pages() -> None:
    """The production transaction shape, which the rollback-conn tests cannot
    reach: batch_size=1 with commit_each=True forces a real conn.commit()
    BETWEEN pages inside one db.transaction() context — the exact seam a
    refactor to the usual with-block pattern would break, and only on the
    second page."""

    class _Recording:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, *, token: str, platform: str, title: str, body: str) -> None:
            self.sent.append(token)

    with _committed_world() as (setup, user_id, first_detection):
        ticket_id = _scalar(
            setup.execute(
                "INSERT INTO tickets (user_id, kind, price_pence, source) "
                "VALUES (%s, 'single', 1280, 'manual') RETURNING id",
                (user_id,),
            )
        )
        second_journey = _scalar(
            setup.execute(
                "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
                "travel_date, scheduled_departure, scheduled_arrival, status) "
                "VALUES (%s, %s, 'LDS', 'KGX', %s, %s, %s, 'assessed') RETURNING id",
                (user_id, ticket_id, _DEP.date(), _DEP, _DEP + timedelta(hours=2)),
            )
        )
        second_detection = _scalar(
            setup.execute(
                "INSERT INTO delay_detections (journey_id, actual_arrival, delay_minutes, "
                "source, band_percent, entitlement_pence) "
                "VALUES (%s, %s, 45, 'hsp', 50, 640) RETURNING id",
                (second_journey, _DEP + timedelta(hours=2, minutes=45)),
            )
        )
        sender = _Recording()

        with db.transaction() as conn:
            stats = notifications.run_notification_sweep(
                conn, sender, batch_size=1, commit_each=True
            )

        assert (stats.notified, stats.pushes) == (2, 2)
        assert len(sender.sent) == 2
        for detection_id in (first_detection, second_detection):
            assert (
                _scalar(
                    setup.execute(
                        "SELECT notified_at FROM delay_detections WHERE id = %s",
                        (detection_id,),
                    )
                )
                is not None
            )


# --- The entrypoint end to end ------------------------------------------------


@pytest.mark.usefixtures("pool")
def test_sweep_once_delivers_and_stamps(caplog: pytest.LogCaptureFixture) -> None:
    with _committed_world() as (setup, _, detection_id):
        with caplog.at_level(logging.INFO, logger="autotrain.sources.push"):
            stats = worker._sweep_once(LogPushSender())

        assert stats.notified == 1
        assert "You're owed £6.40 (50%) for your 08:14 to EUS." in caplog.text
        # The log sender promises the token never appears whole.
        assert _TOKEN not in caplog.text
        assert _TOKEN[:8] in caplog.text
        assert (
            _scalar(
                setup.execute(
                    "SELECT notified_at FROM delay_detections WHERE id = %s", (detection_id,)
                )
            )
            is not None
        )

        # A second pass finds an empty queue: exactly once means exactly once.
        assert worker._sweep_once(LogPushSender()).examined == 0


@pytest.mark.usefixtures("migrated_database")
def test_main_once_runs_and_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The documented operational entrypoint: sender built, pool opened, one
    sweep, pool closed, clean return. No pool fixture — main() owns the pool
    lifecycle itself, and an empty queue is a fine sweep.

    The log assertions are what make this discriminate: main() swallows sweep
    exceptions by design (retry-next-interval), so a worker whose every sweep
    failed would still exit cleanly — only the 'complete' line proves the
    sweep actually ran."""
    monkeypatch.setenv("AUTOTRAIN_PUSH_SENDER", "log")
    monkeypatch.setattr(sys, "argv", ["autotrain-worker", "--once"])
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.INFO, logger="autotrain.entrypoints.worker"):
            worker.main()
    finally:
        # Never leak push_sender=log into other tests' cached settings.
        get_settings.cache_clear()

    assert "notification sweep complete" in caplog.text
    assert "notification sweep failed" not in caplog.text
