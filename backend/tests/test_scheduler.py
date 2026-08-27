"""Scheduler tests: the UK-date expiry boundary as pure unit tests, and
integration passes proving the glue drives both claims jobs end to end —
including the module's headline guarantees: per-job isolation and the
UK-calendar wiring of the expiry boundary.

The integration tests use the `pool` fixture (or main(), which manages its own
pool) because the scheduler borrows pool connections and commits — so unlike
every conn-fixture test they must clean up the rows they committed, in FK
order, even when they fail. `_committed_world` owns that."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest

from autotrain.entrypoints import scheduler
from autotrain.entrypoints.scheduler import _expire_once, _run_jobs_once, _uk_today
from conftest import TEST_DATABASE_URL
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

# --- The expiry boundary -----------------------------------------------------


def test_uk_today_matches_utc_in_winter() -> None:
    # GMT: London == UTC, so the dates agree even late at night.
    assert _uk_today(datetime(2026, 1, 15, 23, 30, tzinfo=UTC)) == date(2026, 1, 15)


def test_uk_today_is_ahead_of_utc_on_summer_nights() -> None:
    """The hour that motivates the function: during BST, 23:30 UTC is already
    00:30 tomorrow in London. Judging expiry by the UTC date would treat a
    claim as still filable for an extra hour past UK midnight on its last
    valid day — the deadline would move at 00:00 UTC instead of UK midnight."""
    assert _uk_today(datetime(2026, 6, 15, 23, 30, tzinfo=UTC)) == date(2026, 6, 16)


def test_uk_today_daytime_agrees_year_round() -> None:
    assert _uk_today(datetime(2026, 6, 15, 12, 0, tzinfo=UTC)) == date(2026, 6, 15)
    assert _uk_today(datetime(2026, 1, 15, 12, 0, tzinfo=UTC)) == date(2026, 1, 15)


# --- Committed scaffolding ---------------------------------------------------


@contextmanager
def _committed_world() -> Iterator[tuple[psycopg.Connection, UUID, UUID]]:
    """An autocommit connection with one consented user and one operator,
    deleted again (FK order: claims RESTRICT journeys and operators; deleting
    journeys cascades detections) even when the test body fails."""
    setup = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    user_id = operator_id = None
    try:
        user_id = _mk_user(setup, "scheduler-test@example.com")
        operator_id = _scalar(
            setup.execute(
                "INSERT INTO operators (atoc_code, name, min_delay_minutes, claim_window_days) "
                "VALUES ('QZ', 'Scheduler Rail', 15, 28) RETURNING id"
            )
        )
        yield setup, user_id, operator_id
    finally:
        if user_id is not None:
            setup.execute("DELETE FROM claims WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM journeys WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM tickets WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM users WHERE id = %s", (user_id,))
        if operator_id is not None:
            setup.execute("DELETE FROM operators WHERE id = %s", (operator_id,))
        setup.close()


def _mk_assessed_journey_with_detection(
    conn: psycopg.Connection, user_id, operator_id, *, travel_date: date, origin: str
) -> tuple:
    """(journey_id, detection_id): an assessed journey with an entitled,
    unclaimed detection — exactly what the claim sweep's work queue holds."""
    departure = datetime.combine(travel_date, datetime.min.time(), tzinfo=UTC).replace(hour=8)
    ticket_id = _scalar(
        conn.execute(
            "INSERT INTO tickets (user_id, kind, price_pence, source) "
            "VALUES (%s, 'single', 4550, 'manual') RETURNING id",
            (user_id,),
        )
    )
    journey_id = _scalar(
        conn.execute(
            "INSERT INTO journeys (user_id, ticket_id, operator_id, origin_crs, "
            "destination_crs, travel_date, scheduled_departure, scheduled_arrival, status) "
            "VALUES (%s, %s, %s, %s, 'EUS', %s, %s, %s, 'assessed') RETURNING id",
            (
                user_id,
                ticket_id,
                operator_id,
                origin,
                travel_date,
                departure,
                departure + timedelta(hours=2),
            ),
        )
    )
    detection_id = _scalar(
        conn.execute(
            "INSERT INTO delay_detections (journey_id, actual_arrival, delay_minutes, "
            "source, band_percent, entitlement_pence) "
            "VALUES (%s, %s, 45, 'hsp', 50, 2275) RETURNING id",
            (journey_id, departure + timedelta(hours=2, minutes=45)),
        )
    )
    return journey_id, detection_id


def _mk_draft_claim(
    conn: psycopg.Connection, user_id, operator_id, *, travel_date: date, file_by: date, origin: str
) -> UUID:
    """A committed 'draft' claim with a chosen deadline, for expiry tests."""
    journey_id, detection_id = _mk_assessed_journey_with_detection(
        conn, user_id, operator_id, travel_date=travel_date, origin=origin
    )
    return _scalar(
        conn.execute(
            "INSERT INTO claims (journey_id, detection_id, operator_id, user_id, "
            "amount_pence, file_by) VALUES (%s, %s, %s, %s, 2275, %s) RETURNING id",
            (journey_id, detection_id, operator_id, user_id, file_by),
        )
    )


def _claim_state(setup: psycopg.Connection, user_id) -> tuple[dict, int]:
    """(journey_id -> status, total claim_events) — the full observable
    outcome of a scheduler pass over this user's claims."""
    statuses = dict(
        setup.execute(
            "SELECT journey_id, status FROM claims WHERE user_id = %s", (user_id,)
        ).fetchall()
    )
    events = _scalar(
        setup.execute(
            "SELECT count(*) FROM claim_events e JOIN claims c ON c.id = e.claim_id "
            "WHERE c.user_id = %s",
            (user_id,),
        )
    )
    return statuses, events


# --- One real pass through both jobs -----------------------------------------


@pytest.mark.usefixtures("pool")
def test_run_jobs_once_opens_then_expires_claims() -> None:
    """One scheduler pass: a fresh journey's detection becomes a draft claim;
    an ancient journey's detection becomes a claim AND is expired in the same
    pass (sweep runs before expiry by design — a back-claim past its window
    gets an audited 'expired', never silence)."""
    with _committed_world() as (setup, user_id, operator_id):
        today = _uk_today(datetime.now(tz=UTC))
        fresh_journey, _ = _mk_assessed_journey_with_detection(
            setup, user_id, operator_id, travel_date=today - timedelta(days=2), origin="MAN"
        )
        stale_journey, _ = _mk_assessed_journey_with_detection(
            setup, user_id, operator_id, travel_date=today - timedelta(days=90), origin="LDS"
        )

        _run_jobs_once(batch_size=50)

        statuses, events = _claim_state(setup, user_id)
        assert statuses == {fresh_journey: "draft", stale_journey: "expired"}
        # draft + (draft, expired): the expiry left an audit trail.
        assert events == 3
        trail = [
            r[0]
            for r in setup.execute(
                "SELECT e.to_status FROM claim_events e "
                "JOIN claims c ON c.id = e.claim_id "
                "WHERE c.journey_id = %s ORDER BY e.created_at",
                (stale_journey,),
            ).fetchall()
        ]
        assert trail == ["draft", "expired"]

        # A second pass is a no-op in FULL: no new claims, no status moved,
        # no event appended — both work queues are genuinely drained.
        _run_jobs_once(batch_size=50)
        assert _claim_state(setup, user_id) == (statuses, events)


@pytest.mark.usefixtures("pool")
def test_run_jobs_once_isolates_a_failing_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The headline containment guarantee: the claim sweep blowing up is
    logged and swallowed, and the expiry job still runs in the same pass."""
    with _committed_world() as (setup, user_id, operator_id):
        today = _uk_today(datetime.now(tz=UTC))
        claim_id = _mk_draft_claim(
            setup,
            user_id,
            operator_id,
            travel_date=today - timedelta(days=90),
            file_by=today - timedelta(days=62),
            origin="MAN",
        )

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("sweep blew up")

        monkeypatch.setattr(scheduler.claims, "run_claim_sweep", _boom)
        with caplog.at_level(logging.ERROR, logger="autotrain.entrypoints.scheduler"):
            _run_jobs_once(batch_size=50)

        assert "claim sweep failed" in caplog.text
        assert (
            _scalar(setup.execute("SELECT status FROM claims WHERE id = %s", (claim_id,)))
            == "expired"
        )


@pytest.mark.usefixtures("pool")
def test_expire_once_judges_by_the_uk_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring the unit tests can't see: _expire_once must feed _uk_today's
    date to the sweep. On a BST deadline night, 22:30 UTC is 23:30 in London
    (deadline day not over — no expiry) while 23:30 UTC is 00:30 tomorrow
    (deadline passed — expire). A UTC-date implementation would refuse both."""
    deadline = date(2026, 6, 15)
    with _committed_world() as (setup, user_id, operator_id):
        claim_id = _mk_draft_claim(
            setup,
            user_id,
            operator_id,
            travel_date=deadline - timedelta(days=28),
            file_by=deadline,
            origin="MAN",
        )

        def _freeze(instant: datetime) -> None:
            monkeypatch.setattr(scheduler, "datetime", SimpleNamespace(now=lambda tz=None: instant))

        _freeze(datetime(2026, 6, 15, 22, 30, tzinfo=UTC))  # 23:30 BST — last valid hour
        assert _expire_once(batch_size=50) == 0

        _freeze(datetime(2026, 6, 15, 23, 30, tzinfo=UTC))  # 00:30 BST June 16 — over
        assert _expire_once(batch_size=50) == 1
        assert (
            _scalar(setup.execute("SELECT status FROM claims WHERE id = %s", (claim_id,)))
            == "expired"
        )


def test_main_once_runs_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented operational entrypoint: argparse --once, pool open,
    both jobs, pool closed, clean return. No pool fixture — main() owns the
    pool lifecycle itself, and the queues being empty is fine (both jobs are
    no-ops then)."""
    monkeypatch.setattr(sys, "argv", ["autotrain-scheduler", "--once"])
    scheduler.main()  # raising (or hanging without --once) is the failure mode
