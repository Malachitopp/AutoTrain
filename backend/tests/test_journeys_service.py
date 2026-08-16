"""Service- and repository-level tests for what the API edge cannot reach.

The schemas mirror every DB constraint, so through HTTP these paths are dead:
driver-error translation, atomicity on a failed second insert, the database
half of the duplicate guard, and list ordering's created_at tiebreak (which is
untestable through the API — now() is transaction-fixed, so rows created in
one test share a created_at). All of it runs against real Postgres via the
rollback `conn` fixture.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import psycopg
import pytest

from autotrain.modules.journeys import repository, service

_DEP = datetime(2026, 8, 10, 8, 14, tzinfo=UTC)
_DAY = date(2026, 8, 10)


def _mk_user(conn: psycopg.Connection, email: str = "svc@example.com") -> UUID:
    row = conn.execute(
        "INSERT INTO users (email, claim_consent_at, claim_consent_terms) "
        "VALUES (%s, now(), 'loa-v1') RETURNING id",
        (email,),
    ).fetchone()
    assert row is not None
    return row[0]


def _tickets_for(conn: psycopg.Connection, user_id: UUID) -> int:
    row = conn.execute("SELECT count(*) FROM tickets WHERE user_id = %s", (user_id,)).fetchone()
    assert row is not None
    return row[0]


def _add(conn: psycopg.Connection, user_id: UUID, **overrides: object) -> object:
    kwargs: dict = {
        "kind": "single",
        "price_pence": 4550,
        "origin_crs": "MAN",
        "destination_crs": "EUS",
        "travel_date": _DAY,
        "scheduled_departure": _DEP,
        "scheduled_arrival": _DEP + timedelta(hours=2),
    }
    kwargs.update(overrides)
    return service.add_manual_journey(conn, user_id, **kwargs)


class TestDriverErrorsNeverEscape:
    """service.py promises psycopg exception types never cross its boundary."""

    def test_unordered_times_raise_invalid_journey_and_leave_no_orphan_ticket(
        self, conn: psycopg.Connection
    ) -> None:
        """The API edge rejects arrival <= departure before SQL runs, so this
        is only reachable by direct callers: the ticket insert succeeds, the
        journey insert hits journeys_times_ordered, and the caller's rollback
        must take the ticket with it — atomicity of the two inserts."""
        uid = _mk_user(conn)
        with pytest.raises(service.InvalidJourney), conn.transaction():
            _add(conn, uid, scheduled_arrival=_DEP)
        # The savepoint rolled back: no journey, and no orphaned ticket either.
        assert _tickets_for(conn, uid) == 0

    def test_price_past_int4_raises_invalid_journey(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        with pytest.raises(service.InvalidJourney), conn.transaction():
            _add(conn, uid, price_pence=3_000_000_000)
        assert _tickets_for(conn, uid) == 0


class TestDuplicateGuardLivesInTheDatabase:
    """The leg_exists pre-check is a fast path; the invariant is the 0009
    unique index, which must hold even when the pre-check misses (the
    check-then-insert race between two concurrent adds)."""

    def test_same_trip_on_a_second_ticket_hits_the_0009_index(
        self, conn: psycopg.Connection
    ) -> None:
        """Every manual add mints a fresh ticket, so only a user-scoped key can
        stop a cross-request duplicate — before 0009 this second insert
        succeeded and produced two monitored journeys for one real trip."""
        uid = _mk_user(conn)
        t1 = repository.insert_ticket(conn, uid, "single", 4550, "manual")
        t2 = repository.insert_ticket(conn, uid, "single", 4550, "manual")
        leg: dict = {
            "user_id": uid,
            "origin_crs": "MAN",
            "destination_crs": "EUS",
            "travel_date": _DAY,
            "scheduled_departure": _DEP,
            "scheduled_arrival": _DEP + timedelta(hours=2),
        }
        repository.insert_journey(conn, ticket_id=t1, **leg)
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo, conn.transaction():
            repository.insert_journey(conn, ticket_id=t2, **leg)
        # Pin the constraint NAME: the service's translation matches on it, so
        # a rename in a future migration must fail here loudly, not turn the
        # race loser's 409 into a 500.
        assert excinfo.value.diag.constraint_name == "journeys_user_leg_departure_key"

    def test_same_ticket_same_leg_hits_the_0005_key(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        tid = repository.insert_ticket(conn, uid, "single", 4550, "manual")
        leg: dict = {
            "user_id": uid,
            "ticket_id": tid,
            "origin_crs": "MAN",
            "destination_crs": "EUS",
            "travel_date": _DAY,
            "scheduled_arrival": _DEP + timedelta(hours=2),
        }
        repository.insert_journey(conn, scheduled_departure=_DEP, **leg)
        # A different departure dodges the 0009 index, so the ticket-scoped
        # 0005 key is the one that fires.
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo, conn.transaction():
            repository.insert_journey(conn, scheduled_departure=_DEP + timedelta(hours=1), **leg)
        assert excinfo.value.diag.constraint_name == "journeys_ticket_leg_key"

    def test_race_loser_gets_duplicate_journey_not_a_driver_error(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates losing the race: the pre-check reports "not there" (as it
        does for both requests inside the race window) and the INSERT then
        hits the real index. The database is not mocked — only the fast-path
        SELECT is skipped, exactly what concurrency does to it."""
        uid = _mk_user(conn)
        with conn.transaction():
            _add(conn, uid)
        monkeypatch.setattr(repository, "leg_exists", lambda *args: False)
        with pytest.raises(service.DuplicateJourney), conn.transaction():
            _add(conn, uid)

    def test_sequential_duplicate_is_caught_by_the_precheck(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        with conn.transaction():
            _add(conn, uid)
        with pytest.raises(service.DuplicateJourney), conn.transaction():
            _add(conn, uid)


class TestListOrdering:
    def test_created_at_breaks_same_day_ties_newest_first(self, conn: psycopg.Connection) -> None:
        """travel_date DESC, created_at DESC. now() is fixed for the whole
        transaction, so distinct created_at values must be inserted explicitly
        — which is also why no API-level test can cover this tiebreak."""
        uid = _mk_user(conn)
        ids: list[UUID] = []
        for hour_offset, dep_hour in ((0, 8), (1, 18)):  # later-created second
            tid = repository.insert_ticket(conn, uid, "single", 4550, "manual")
            row = conn.execute(
                "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
                "travel_date, scheduled_departure, scheduled_arrival, created_at) "
                "VALUES (%s, %s, 'MAN', 'EUS', %s, %s, %s, %s) RETURNING id",
                (
                    uid,
                    tid,
                    _DAY,
                    _DEP.replace(hour=dep_hour),
                    _DEP.replace(hour=dep_hour) + timedelta(hours=2),
                    datetime(2026, 8, 1, 10, tzinfo=UTC) + timedelta(hours=hour_offset),
                ),
            ).fetchone()
            assert row is not None
            ids.append(row[0])

        rows = repository.list_for_user(conn, uid, 50)
        assert [r.id for r in rows] == [ids[1], ids[0]]  # newest created_at first
        # LIMIT truncates from the top: the newest row survives the cut.
        assert repository.list_for_user(conn, uid, 1) == [rows[0]]
