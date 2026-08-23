"""Delay sweep tests: entitlement maths as pure unit tests, sweep behaviour
against real Postgres via the rollback `conn` fixture.

Tests build their own operator ('QQ' — not a real TOC, so it can never collide
with seeded reference data) with an explicit band scale, so every expected
pence value here is checkable by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest

from autotrain.modules.delays.service import (
    ArrivalReport,
    AssessableJourney,
    Band,
    compute_entitlement,
    run_sweep,
)
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

# The standard national scale: 25% at 15-29, 50% at 30-59, 100% at 60-119,
# 100% of the RETURN fare at 120+.
BANDS = (
    Band(15, 30, 25, of_return_fare=False),
    Band(30, 60, 50, of_return_fare=False),
    Band(60, 120, 100, of_return_fare=False),
    Band(120, None, 100, of_return_fare=True),
)

NOW = datetime.now(tz=UTC)
# Old enough to clear any plausible ingestor lag; recent enough that the
# default give-up window has not passed.
ARRIVAL = NOW - timedelta(hours=30)
DEPARTURE = ARRIVAL - timedelta(hours=2)
TRAVEL_DATE = DEPARTURE.date()
CUTOFF = NOW
GIVE_UP_BEFORE = TRAVEL_DATE - timedelta(days=7)


class FakeSource:
    """Maps journey id -> report; None (unknown) for everything else.
    Journey ids listed in `explode` raise instead, like a network failure.
    `seen` records the order journeys were asked about — the sweep's
    oldest-first contract is asserted through it."""

    def __init__(
        self,
        reports: dict[UUID, ArrivalReport] | None = None,
        explode: set[UUID] | None = None,
    ) -> None:
        self.reports = reports or {}
        self.explode = explode or set()
        self.seen: list[UUID] = []

    def actual_arrival(self, journey: AssessableJourney) -> ArrivalReport | None:
        self.seen.append(journey.id)
        if journey.id in self.explode:
            raise RuntimeError("source blew up")
        return self.reports.get(journey.id)


def _mk_ticket(
    conn: psycopg.Connection, user_id: UUID, kind: str = "single", price_pence: int = 4550
) -> UUID:
    return _scalar(
        conn.execute(
            "INSERT INTO tickets (user_id, kind, price_pence, source) "
            "VALUES (%s, %s, %s, 'manual') RETURNING id",
            (user_id, kind, price_pence),
        )
    )


def _mk_journey(
    conn: psycopg.Connection,
    user_id: UUID,
    ticket_id: UUID,
    *,
    operator_id: UUID | None = None,
    departure: datetime = DEPARTURE,
    arrival: datetime = ARRIVAL,
    origin: str = "MAN",
    destination: str = "EUS",
) -> UUID:
    return _scalar(
        conn.execute(
            "INSERT INTO journeys (user_id, ticket_id, operator_id, origin_crs, "
            "destination_crs, travel_date, scheduled_departure, scheduled_arrival) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                user_id,
                ticket_id,
                operator_id,
                origin,
                destination,
                departure.date(),
                departure,
                arrival,
            ),
        )
    )


def _mk_operator(
    conn: psycopg.Connection,
    atoc: str = "QQ",
    min_delay: int = 15,
    bands: tuple[Band, ...] = BANDS,
) -> UUID:
    op_id = _scalar(
        conn.execute(
            "INSERT INTO operators (atoc_code, name, min_delay_minutes) "
            "VALUES (%s, 'Test Railways', %s) RETURNING id",
            (atoc, min_delay),
        )
    )
    for band in bands:
        conn.execute(
            "INSERT INTO delay_repay_bands "
            "(operator_id, min_minutes, max_minutes, percent, of_return_fare) "
            "VALUES (%s, %s, %s, %s, %s)",
            (op_id, band.min_minutes, band.max_minutes, band.percent, band.of_return_fare),
        )
    return op_id


def _detection(conn: psycopg.Connection, journey_id: UUID) -> tuple | None:
    return conn.execute(
        "SELECT delay_minutes, source, band_percent, entitlement_pence, notified_at "
        "FROM delay_detections WHERE journey_id = %s",
        (journey_id,),
    ).fetchone()


def _status(conn: psycopg.Connection, journey_id: UUID) -> str:
    row = conn.execute("SELECT status FROM journeys WHERE id = %s", (journey_id,)).fetchone()
    assert row is not None
    return row[0]


class TestComputeEntitlement:
    def test_below_operator_minimum_is_nothing(self) -> None:
        assert compute_entitlement(
            BANDS, delay_minutes=14, min_delay_minutes=15, price_pence=4550, ticket_kind="single"
        ) == (None, 0)

    @pytest.mark.parametrize(
        ("delay", "percent", "pence"),
        [
            (15, 25, 1137),  # 4550 * 25% = 1137.5 → floored, never rounded up
            (29, 25, 1137),  # last minute of the first band
            (30, 50, 2275),  # first minute of the second
            (119, 100, 4550),
        ],
    )
    def test_band_edges_on_a_single(self, delay: int, percent: int, pence: int) -> None:
        assert compute_entitlement(
            BANDS, delay_minutes=delay, min_delay_minutes=15, price_pence=4550, ticket_kind="single"
        ) == (percent, pence)

    def test_return_pays_on_half_the_fare(self) -> None:
        # One delayed leg of a £100.00 return at 50% = 50% of £50.00.
        assert compute_entitlement(
            BANDS, delay_minutes=30, min_delay_minutes=15, price_pence=10000, ticket_kind="return"
        ) == (50, 2500)

    def test_top_band_pays_on_the_full_return_fare(self) -> None:
        assert compute_entitlement(
            BANDS, delay_minutes=120, min_delay_minutes=15, price_pence=10000, ticket_kind="return"
        ) == (100, 10000)

    def test_season_is_never_priced(self) -> None:
        assert compute_entitlement(
            BANDS, delay_minutes=60, min_delay_minutes=15, price_pence=100000, ticket_kind="season"
        ) == (None, 0)

    def test_gap_between_minimum_and_first_band_entitles_nothing(self) -> None:
        # An operator whose scheme starts at 30 but whose minimum is 15:
        # 20 minutes is past the minimum yet inside no band.
        late_bands = (Band(30, None, 50, of_return_fare=False),)
        assert compute_entitlement(
            late_bands,
            delay_minutes=20,
            min_delay_minutes=15,
            price_pence=4550,
            ticket_kind="single",
        ) == (None, 0)

    def test_no_bands_at_all_entitles_nothing(self) -> None:
        assert compute_entitlement(
            (), delay_minutes=60, min_delay_minutes=15, price_pence=4550, ticket_kind="single"
        ) == (None, 0)


class TestSweep:
    def test_late_journey_gets_a_priced_detection(self, conn: psycopg.Connection) -> None:
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op)
        source = FakeSource(
            {jid: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=35), source="hsp")}
        )

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert (stats.examined, stats.assessed, stats.entitled) == (1, 1, 1)
        assert _detection(conn, jid) == (35, "hsp", 50, 2275, None)
        assert _status(conn, jid) == "assessed"

    def test_on_time_journey_is_closed_with_zero(self, conn: psycopg.Connection) -> None:
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op)
        source = FakeSource({jid: ArrivalReport(actual_arrival=ARRIVAL, source="hsp")})

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert (stats.assessed, stats.entitled) == (1, 0)
        assert _detection(conn, jid) == (0, "hsp", None, 0, None)
        assert _status(conn, jid) == "assessed"

    def test_second_sweep_has_nothing_to_do(self, conn: psycopg.Connection) -> None:
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op)
        source = FakeSource(
            {jid: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=35), source="hsp")}
        )

        first = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)
        second = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert first.assessed == 1
        assert second.examined == 0  # assessed journeys leave the work query
        n = conn.execute(
            "SELECT count(*) FROM delay_detections WHERE journey_id = %s", (jid,)
        ).fetchone()
        assert n is not None and n[0] == 1

    def test_unknown_recent_journey_is_left_for_the_next_sweep(
        self, conn: psycopg.Connection
    ) -> None:
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))

        stats = run_sweep(conn, FakeSource(), cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert (stats.no_data, stats.gave_up) == (1, 0)
        assert _detection(conn, jid) is None
        assert _status(conn, jid) == "pending"

    def test_unknown_old_journey_is_marked_unmatched(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))

        stats = run_sweep(
            conn,
            FakeSource(),
            cutoff=CUTOFF,
            # The journey's travel date is already before this line.
            give_up_before=TRAVEL_DATE + timedelta(days=1),
        )

        assert stats.gave_up == 1
        assert _detection(conn, jid) is None
        assert _status(conn, jid) == "unmatched"

    def test_future_journey_is_not_swept(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        _mk_journey(
            conn,
            uid,
            _mk_ticket(conn, uid),
            departure=NOW + timedelta(hours=1),
            arrival=NOW + timedelta(hours=3),
        )

        stats = run_sweep(conn, FakeSource(), cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert stats.examined == 0

    def test_atoc_code_resolves_the_operator_and_prices(self, conn: psycopg.Connection) -> None:
        op = _mk_operator(conn, atoc="QQ")
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))  # no operator on the journey
        source = FakeSource(
            {
                jid: ArrivalReport(
                    actual_arrival=ARRIVAL + timedelta(minutes=65), source="hsp", atoc_code="QQ"
                )
            }
        )

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert stats.entitled == 1
        assert _detection(conn, jid) == (65, "hsp", 100, 4550, None)
        row = conn.execute("SELECT operator_id FROM journeys WHERE id = %s", (jid,)).fetchone()
        assert row is not None and row[0] == op

    def test_no_operator_records_the_delay_but_prices_nothing(
        self, conn: psycopg.Connection
    ) -> None:
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))  # no operator, report has no atoc
        source = FakeSource(
            {jid: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=35), source="hsp")}
        )

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert (stats.assessed, stats.entitled) == (1, 0)
        assert _detection(conn, jid) == (35, "hsp", None, 0, None)
        assert _status(conn, jid) == "assessed"

    def test_one_bad_journey_does_not_stop_the_sweep(self, conn: psycopg.Connection) -> None:
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        # The exploding journey sorts first (earlier arrival), proving the
        # sweep recovers and still processes the one after it.
        bad = _mk_journey(
            conn,
            uid,
            _mk_ticket(conn, uid),
            operator_id=op,
            departure=DEPARTURE - timedelta(hours=1),
            arrival=ARRIVAL - timedelta(hours=1),
            origin="LDS",
        )
        good = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op)
        source = FakeSource(
            {good: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=20), source="hsp")},
            explode={bad},
        )

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert source.seen[0] == bad  # proves recovery happened AFTER a failure
        assert (stats.examined, stats.errors, stats.assessed) == (2, 1, 1)
        assert _detection(conn, bad) is None
        assert _status(conn, bad) == "pending"  # savepoint rolled its work back
        assert _detection(conn, good) == (20, "hsp", 25, 1137, None)

    def test_small_pages_still_drain_the_whole_backlog_oldest_first(
        self, conn: psycopg.Connection
    ) -> None:
        # batch_size is a page size, not a cap: journeys the source cannot
        # answer for must not block the ones behind them (keyset pagination),
        # and the sweep must visit oldest arrivals first.
        uid = _mk_user(conn)
        jids = [
            _mk_journey(
                conn,
                uid,
                _mk_ticket(conn, uid),
                departure=DEPARTURE + timedelta(minutes=i),
                arrival=ARRIVAL + timedelta(minutes=i),
            )
            for i in range(3)
        ]
        source = FakeSource()

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE, batch_size=2)

        assert stats.examined == 3
        assert source.seen == jids  # oldest scheduled arrival first

    def test_matched_journey_is_assessed_too(self, conn: psycopg.Connection) -> None:
        # 'matched' is the primary monitored population once the Darwin
        # matcher exists (0005) — the sweep must not be pending-only.
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op)
        conn.execute("UPDATE journeys SET status = 'matched' WHERE id = %s", (jid,))
        source = FakeSource(
            {jid: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=35), source="hsp")}
        )

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert stats.assessed == 1
        assert _status(conn, jid) == "assessed"

    def test_matched_journey_is_never_retired_to_unmatched(self, conn: psycopg.Connection) -> None:
        # 'unmatched' means "we gave up matching" (0005). A journey already
        # matched to a service lacking source data is the source's gap, not a
        # matching failure — it stays matched however old it gets.
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))
        conn.execute("UPDATE journeys SET status = 'matched' WHERE id = %s", (jid,))

        stats = run_sweep(
            conn, FakeSource(), cutoff=CUTOFF, give_up_before=TRAVEL_DATE + timedelta(days=1)
        )

        assert (stats.no_data, stats.gave_up) == (1, 0)
        assert _status(conn, jid) == "matched"

    def test_travel_exactly_at_the_give_up_boundary_is_retired(
        self, conn: psycopg.Connection
    ) -> None:
        # give_up_days must mean what the config says: travelled exactly that
        # many days ago = retired (<=, not <).
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))

        stats = run_sweep(conn, FakeSource(), cutoff=CUTOFF, give_up_before=TRAVEL_DATE)

        assert stats.gave_up == 1
        assert _status(conn, jid) == "unmatched"

    def test_season_journeys_are_deferred_not_assessed(self, conn: psycopg.Connection) -> None:
        # Closing a season at 0 pence would forfeit money genuinely owed; the
        # work query excludes them entirely, whatever their age.
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid, kind="season"))

        stats = run_sweep(
            conn, FakeSource(), cutoff=CUTOFF, give_up_before=TRAVEL_DATE + timedelta(days=1)
        )

        assert stats.examined == 0
        assert _detection(conn, jid) is None
        assert _status(conn, jid) == "pending"

    def test_existing_operator_is_never_overridden_by_the_report(
        self, conn: psycopg.Connection
    ) -> None:
        # The journey's operator is authoritative. The report's TOC would
        # price differently (10% single band) — entitlement must come from
        # the journey's operator scale.
        op_a = _mk_operator(conn, atoc="QQ")
        # A second operator whose scheme would price differently (10%) — its
        # bands must NOT be used.
        _mk_operator(conn, atoc="QX", bands=(Band(15, None, 10, of_return_fare=False),))
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op_a)
        source = FakeSource(
            {
                jid: ArrivalReport(
                    actual_arrival=ARRIVAL + timedelta(minutes=35), source="hsp", atoc_code="QX"
                )
            }
        )

        run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert _detection(conn, jid) == (35, "hsp", 50, 2275, None)  # op_a's 30-59 band
        row = conn.execute("SELECT operator_id FROM journeys WHERE id = %s", (jid,)).fetchone()
        assert row is not None and row[0] == op_a

    def test_source_wide_failure_retires_nothing(self, conn: psycopg.Connection) -> None:
        # Bad credentials or an HSP outage = EVERY call raises. The journeys
        # are fine; the source is not. Give-up must not fire, or a week-long
        # credential typo silently forfeits the whole backlog.
        uid = _mk_user(conn)
        old = {
            _mk_journey(
                conn,
                uid,
                _mk_ticket(conn, uid),
                departure=DEPARTURE + timedelta(minutes=i),
                arrival=ARRIVAL + timedelta(minutes=i),
            )
            for i in range(2)
        }
        source = FakeSource(explode=old)

        stats = run_sweep(
            conn, source, cutoff=CUTOFF, give_up_before=TRAVEL_DATE + timedelta(days=1)
        )

        assert (stats.errors, stats.gave_up) == (2, 0)
        for jid in old:
            assert _status(conn, jid) == "pending"

    def test_poison_journey_is_retired_once_the_source_proves_alive(
        self, conn: psycopg.Connection
    ) -> None:
        # One journey deterministically errors while another succeeds: the
        # failure is journey-scoped, so the old poison journey ages out.
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        bad = _mk_journey(conn, uid, _mk_ticket(conn, uid), origin="LDS")
        good = _mk_journey(
            conn,
            uid,
            _mk_ticket(conn, uid),
            operator_id=op,
            departure=DEPARTURE + timedelta(minutes=5),
            arrival=ARRIVAL + timedelta(minutes=5),
        )
        source = FakeSource(
            {good: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=25), source="hsp")},
            explode={bad},
        )

        stats = run_sweep(
            conn, source, cutoff=CUTOFF, give_up_before=TRAVEL_DATE + timedelta(days=1)
        )

        assert (stats.errors, stats.assessed, stats.gave_up) == (1, 1, 1)
        assert _status(conn, bad) == "unmatched"
        assert _status(conn, good) == "assessed"

    def test_sweep_aborts_when_every_early_journey_fails(self, conn: psycopg.Connection) -> None:
        # First N journeys all erroring = the source is down; stop paying a
        # timeout per journey to keep learning it.
        uid = _mk_user(conn)
        jids = {
            _mk_journey(
                conn,
                uid,
                _mk_ticket(conn, uid),
                departure=DEPARTURE + timedelta(minutes=i),
                arrival=ARRIVAL + timedelta(minutes=i),
            )
            for i in range(5)
        }
        source = FakeSource(explode=jids)

        stats = run_sweep(
            conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE, abort_error_streak=3
        )

        assert (stats.examined, stats.errors) == (3, 3)

    def test_existing_detection_is_kept_and_counted_as_lost_race(
        self, conn: psycopg.Connection
    ) -> None:
        # A writer that crashed between its detection INSERT and its status
        # claim leaves a pending journey WITH a detection. The sweep must
        # repair the status but keep the frozen decision (ON CONFLICT DO
        # NOTHING rowcount 0 = stop, per 0006).
        op = _mk_operator(conn)
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid), operator_id=op)
        conn.execute(
            "INSERT INTO delay_detections "
            "(journey_id, actual_arrival, delay_minutes, source, band_percent, entitlement_pence) "
            "VALUES (%s, %s, 20, 'darwin', 25, 1137)",
            (jid, ARRIVAL + timedelta(minutes=20)),
        )
        source = FakeSource(
            {jid: ArrivalReport(actual_arrival=ARRIVAL + timedelta(minutes=95), source="hsp")}
        )

        stats = run_sweep(conn, source, cutoff=CUTOFF, give_up_before=GIVE_UP_BEFORE)

        assert (stats.lost_race, stats.assessed) == (1, 0)
        assert _status(conn, jid) == "assessed"  # repaired
        assert _detection(conn, jid) == (20, "darwin", 25, 1137, None)  # frozen, not ours
