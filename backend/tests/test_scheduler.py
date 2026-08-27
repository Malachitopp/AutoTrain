"""Scheduler tests: the UK-date expiry boundary as pure unit tests, and one
integration pass proving the glue drives both claims jobs end to end.

The integration test uses the `pool` fixture because _run_jobs_once borrows
pool connections and commits — so unlike every conn-fixture test it must clean
up the rows it committed, in FK order, even when it fails."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from autotrain.entrypoints.scheduler import _run_jobs_once, _uk_today
from conftest import TEST_DATABASE_URL
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

# --- The expiry boundary -----------------------------------------------------


def test_uk_today_matches_utc_in_winter() -> None:
    # GMT: London == UTC, so the dates agree even late at night.
    assert _uk_today(datetime(2026, 1, 15, 23, 30, tzinfo=UTC)) == date(2026, 1, 15)


def test_uk_today_is_ahead_of_utc_on_summer_nights() -> None:
    """The hour that motivates the function: during BST, 23:30 UTC is already
    00:30 tomorrow in London. Judging expiry by the UTC date here would treat
    a claim as still filable an extra hour — or, on the flip side at the
    give-up boundary, expire it an hour early."""
    assert _uk_today(datetime(2026, 6, 15, 23, 30, tzinfo=UTC)) == date(2026, 6, 16)


def test_uk_today_daytime_agrees_year_round() -> None:
    assert _uk_today(datetime(2026, 6, 15, 12, 0, tzinfo=UTC)) == date(2026, 6, 15)
    assert _uk_today(datetime(2026, 1, 15, 12, 0, tzinfo=UTC)) == date(2026, 1, 15)


# --- One real pass through both jobs -----------------------------------------


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


@pytest.mark.usefixtures("pool")
def test_run_jobs_once_opens_then_expires_claims() -> None:
    """One scheduler pass: a fresh journey's detection becomes a draft claim;
    an ancient journey's detection becomes a claim AND is expired in the same
    pass (sweep runs before expiry by design — a back-claim past its window
    gets an audited 'expired', never silence)."""
    setup = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    today = _uk_today(datetime.now(tz=UTC))
    user_id = operator_id = None
    try:
        user_id = _mk_user(setup, "scheduler-test@example.com")
        operator_id = _scalar(
            setup.execute(
                "INSERT INTO operators (atoc_code, name, min_delay_minutes, claim_window_days) "
                "VALUES ('QZ', 'Scheduler Rail', 15, 28) RETURNING id"
            )
        )
        fresh_journey, _ = _mk_assessed_journey_with_detection(
            setup, user_id, operator_id, travel_date=today - timedelta(days=2), origin="MAN"
        )
        stale_journey, _ = _mk_assessed_journey_with_detection(
            setup, user_id, operator_id, travel_date=today - timedelta(days=90), origin="LDS"
        )

        _run_jobs_once(batch_size=50)

        statuses = dict(
            setup.execute(
                "SELECT journey_id, status FROM claims WHERE user_id = %s", (user_id,)
            ).fetchall()
        )
        assert statuses == {fresh_journey: "draft", stale_journey: "expired"}
        # The expiry left an audit trail, exactly like any other transition.
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

        # A second pass is a no-op: both work queues are drained.
        _run_jobs_once(batch_size=50)
        assert (
            _scalar(setup.execute("SELECT count(*) FROM claims WHERE user_id = %s", (user_id,)))
            == 2
        )
    finally:
        # Committed rows: remove them even on failure, in FK order
        # (claims RESTRICT journeys and operators; detections cascade).
        if user_id is not None:
            setup.execute("DELETE FROM claims WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM journeys WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM tickets WHERE user_id = %s", (user_id,))
            setup.execute("DELETE FROM users WHERE id = %s", (user_id,))
        if operator_id is not None:
            setup.execute("DELETE FROM operators WHERE id = %s", (operator_id,))
        setup.close()
