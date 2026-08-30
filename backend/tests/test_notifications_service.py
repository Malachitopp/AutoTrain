"""Notification sweep tests: the message, the exactly-once stamp, and the
queue semantics — all against real Postgres via the rollback `conn` fixture.

The sender is a test double, and that is the ArrivalsSource precedent, not a
broken rule: "never mock the database" is about SQL, and the sender is the
external transport — exactly the seam the PushSender protocol exists to
fake. Every stamp, lock and queue read here is real SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg

from autotrain.modules.notifications.service import run_notification_sweep
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

# 15 June is BST: departures below are stored in UTC and must render an hour
# later as UK wall-clock time in the message.
_SUMMER_DEP_UTC = datetime(2026, 6, 15, 7, 14, tzinfo=UTC)  # 08:14 in London
_WINTER_DEP_UTC = datetime(2026, 1, 15, 8, 14, tzinfo=UTC)  # 08:14 in London

# PLAN §3's example figure: £6.40 at the 50% band.
ENTITLEMENT = 640


class _RecordingSender:
    """PushSender double that remembers every delivery."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    def send(self, *, token: str, platform: str, title: str, body: str) -> None:
        self.sent.append((token, platform, title, body))


class _RefusingSender:
    """PushSender double whose deliveries all fail."""

    def send(self, *, token: str, platform: str, title: str, body: str) -> None:
        raise RuntimeError("push provider is down")


# --- Row builders ------------------------------------------------------------


def _mk_journey(
    conn: psycopg.Connection,
    user_id: UUID,
    *,
    departure: datetime = _SUMMER_DEP_UTC,
    origin: str = "MAN",
    destination: str = "EUS",
) -> UUID:
    ticket_id = _scalar(
        conn.execute(
            "INSERT INTO tickets (user_id, kind, price_pence, source) "
            "VALUES (%s, 'single', 1280, 'manual') RETURNING id",
            (user_id,),
        )
    )
    return _scalar(
        conn.execute(
            "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
            "travel_date, scheduled_departure, scheduled_arrival, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'assessed') RETURNING id",
            (
                user_id,
                ticket_id,
                origin,
                destination,
                departure.date(),
                departure,
                departure + timedelta(hours=2),
            ),
        )
    )


def _mk_detection(
    conn: psycopg.Connection,
    journey_id: UUID,
    *,
    entitlement_pence: int = ENTITLEMENT,
    band_percent: int | None = 50,
    observed_at: datetime | None = None,
) -> UUID:
    # observed_at defaults to now(), which is TRANSACTION time — every
    # detection a test creates shares it, so queue order tie-breaks on random
    # uuids. A test that depends on which row heads the queue must say so.
    return _scalar(
        conn.execute(
            "INSERT INTO delay_detections (journey_id, actual_arrival, delay_minutes, "
            "source, band_percent, entitlement_pence, observed_at) "
            "VALUES (%s, now(), 45, 'hsp', %s, %s, COALESCE(%s, now())) RETURNING id",
            (journey_id, band_percent, entitlement_pence, observed_at),
        )
    )


def _mk_device(
    conn: psycopg.Connection, user_id: UUID, *, token: str, platform: str = "ios"
) -> None:
    conn.execute(
        "INSERT INTO devices (user_id, platform, push_token) VALUES (%s, %s, %s)",
        (user_id, platform, token),
    )


def _notified_at(conn: psycopg.Connection, detection_id: UUID) -> datetime | None:
    return _scalar(
        conn.execute("SELECT notified_at FROM delay_detections WHERE id = %s", (detection_id,))
    )


# --- The message -------------------------------------------------------------


def test_sweep_sends_the_plan_sentence_and_stamps(conn: psycopg.Connection) -> None:
    """The headline path, down to the exact PLAN §3 wording — including the
    BST rendering: 07:14 UTC is the user's 08:14 to Euston."""
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-ios-1")
    detection_id = _mk_detection(conn, _mk_journey(conn, user_id))
    sender = _RecordingSender()

    stats = run_notification_sweep(conn, sender)

    assert (stats.examined, stats.notified, stats.pushes, stats.errors) == (1, 1, 1, 0)
    (token, platform, title, body) = sender.sent[0]
    assert (token, platform, title) == ("tok-ios-1", "ios", "Delay Repay")
    assert body == "You're owed £6.40 (50%) for your 08:14 to EUS."
    assert _notified_at(conn, detection_id) is not None


def test_message_in_winter_matches_utc(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    _mk_detection(conn, _mk_journey(conn, user_id, departure=_WINTER_DEP_UTC))
    sender = _RecordingSender()

    run_notification_sweep(conn, sender)

    assert "for your 08:14 to EUS." in sender.sent[0][3]


def test_message_without_a_band_omits_the_percent(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    _mk_detection(conn, _mk_journey(conn, user_id), band_percent=None)
    sender = _RecordingSender()

    run_notification_sweep(conn, sender)

    assert sender.sent[0][3] == "You're owed £6.40 for your 08:14 to EUS."


def test_money_renders_from_integer_pence(conn: psycopg.Connection) -> None:
    """£0.05 and £12.00 — the two formats float arithmetic gets wrong."""
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    _mk_detection(conn, _mk_journey(conn, user_id, origin="LDS"), entitlement_pence=5)
    _mk_detection(
        conn,
        _mk_journey(conn, user_id, origin="YRK", departure=_SUMMER_DEP_UTC + timedelta(hours=1)),
        entitlement_pence=1200,
    )
    sender = _RecordingSender()

    run_notification_sweep(conn, sender)

    bodies = sorted(body for (_, _, _, body) in sender.sent)
    assert any("£0.05" in body for body in bodies)
    assert any("£12.00" in body for body in bodies)


# --- Exactly-once and the queue ----------------------------------------------


def test_sweep_is_idempotent(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    _mk_detection(conn, _mk_journey(conn, user_id))
    sender = _RecordingSender()

    first = run_notification_sweep(conn, sender)
    second = run_notification_sweep(conn, sender)

    assert (first.notified, second.examined) == (1, 0)
    assert len(sender.sent) == 1


def test_under_threshold_detections_are_never_notified(conn: psycopg.Connection) -> None:
    """entitlement 0 is 'late but under threshold' (0006): recorded, never
    notified — and never stamped, since nothing should process it."""
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    detection_id = _mk_detection(conn, _mk_journey(conn, user_id), entitlement_pence=0)
    sender = _RecordingSender()

    stats = run_notification_sweep(conn, sender)

    assert stats.examined == 0
    assert sender.sent == []
    assert _notified_at(conn, detection_id) is None


def test_no_device_retires_the_detection_without_a_push(conn: psycopg.Connection) -> None:
    """Tokens arrive when the app is installed; week-old news must not greet
    the install as a push storm. The stamp without a send is the record."""
    user_id = _mk_user(conn)
    detection_id = _mk_detection(conn, _mk_journey(conn, user_id))
    sender = _RecordingSender()

    stats = run_notification_sweep(conn, sender)

    assert (stats.no_target, stats.pushes) == (1, 0)
    assert _notified_at(conn, detection_id) is not None
    # ...and a device registered later does not resurrect it.
    _mk_device(conn, user_id, token="tok-late")
    assert run_notification_sweep(conn, sender).examined == 0


def test_every_device_gets_the_push(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-phone", platform="ios")
    _mk_device(conn, user_id, token="tok-tablet", platform="android")
    _mk_detection(conn, _mk_journey(conn, user_id))
    sender = _RecordingSender()

    stats = run_notification_sweep(conn, sender)

    assert (stats.notified, stats.pushes) == (1, 2)
    assert {t for (t, _, _, _) in sender.sent} == {"tok-phone", "tok-tablet"}


def test_failed_send_is_not_stamped_and_retries_next_sweep(conn: psycopg.Connection) -> None:
    """The 0006 contract: the stamp commits with the send, so a failed send
    rolls the stamp back and the detection stays queued — silence is money
    lost, a retry is merely annoying."""
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    detection_id = _mk_detection(conn, _mk_journey(conn, user_id))

    stats = run_notification_sweep(conn, _RefusingSender())

    assert (stats.errors, stats.notified) == (1, 0)
    assert _notified_at(conn, detection_id) is None

    recovered = _RecordingSender()
    assert run_notification_sweep(conn, recovered).notified == 1
    assert len(recovered.sent) == 1


def test_one_failing_detection_does_not_block_the_rest(conn: psycopg.Connection) -> None:
    class _OneBadToken:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, *, token: str, platform: str, title: str, body: str) -> None:
            if token == "tok-bad":
                raise RuntimeError("this device is cursed")
            self.sent.append(token)

    unlucky = _mk_user(conn, "unlucky@example.com")
    lucky = _mk_user(conn, "lucky@example.com")
    _mk_device(conn, unlucky, token="tok-bad")
    _mk_device(conn, lucky, token="tok-good")
    # The bad detection must deterministically HEAD the queue: this test
    # exists to prove the widened fetch sees past a poisoned first row, and
    # with a shared transaction-time observed_at the order would be a uuid
    # coin flip — half of all runs would never enter the widening path.
    bad_detection = _mk_detection(
        conn, _mk_journey(conn, unlucky), observed_at=_SUMMER_DEP_UTC + timedelta(hours=3)
    )
    good_detection = _mk_detection(conn, _mk_journey(conn, lucky, origin="LDS"))
    sender = _OneBadToken()

    stats = run_notification_sweep(conn, sender, batch_size=1)

    assert (stats.notified, stats.errors) == (1, 1)
    assert sender.sent == ["tok-good"]
    assert _notified_at(conn, good_detection) is not None
    assert _notified_at(conn, bad_detection) is None


def test_partial_multi_device_failure_keeps_the_detection_queued(
    conn: psycopg.Connection,
) -> None:
    """The module docstring's explicit trade, pinned: when one of a user's
    devices fails, the detection is NOT stamped — the retry will push the
    successful device again (annoying) rather than letting the failed device
    never hear about the money (silence)."""

    class _SecondSendFails:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, *, token: str, platform: str, title: str, body: str) -> None:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("apns hiccup")

    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-phone", platform="ios")
    _mk_device(conn, user_id, token="tok-tablet", platform="android")
    detection_id = _mk_detection(conn, _mk_journey(conn, user_id))

    stats = run_notification_sweep(conn, _SecondSendFails())

    assert (stats.errors, stats.notified) == (1, 0)
    assert _notified_at(conn, detection_id) is None

    # The retry delivers to BOTH devices — the documented duplicate residue.
    recovered = _RecordingSender()
    assert run_notification_sweep(conn, recovered).notified == 1
    assert len(recovered.sent) == 2


def test_sweep_pages_through_more_than_one_batch(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    _mk_device(conn, user_id, token="tok-1")
    for index, origin in enumerate(("MAN", "LDS", "YRK")):
        _mk_detection(
            conn,
            _mk_journey(
                conn,
                user_id,
                origin=origin,
                departure=_SUMMER_DEP_UTC + timedelta(hours=index),
            ),
        )
    sender = _RecordingSender()

    stats = run_notification_sweep(conn, sender, batch_size=2)

    assert (stats.examined, stats.notified) == (3, 3)
    assert len(sender.sent) == 3
