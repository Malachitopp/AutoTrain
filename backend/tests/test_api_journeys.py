"""API tests for the journeys vertical slice.

The client drives HTTP through the real app — routing, validation, service,
repository, real Postgres — with exactly one substitution: `get_conn` is
overridden to the suite's rollback `conn` fixture, so these tests leave the
database untouched and the connection pool is never opened. No mocks anywhere.

The exception is TestProductionTransactionPath at the bottom: it runs the real
wiring (lifespan, pool, TransactionMiddleware commit) with no overrides, so
the code path that actually persists data in production is executed by at
least one test. It commits for real and cleans up after itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from autotrain.api.app import create_app
from autotrain.api.deps import get_conn

_DEP = datetime(2026, 8, 10, 8, 14, tzinfo=UTC)


def _mk_user(conn: psycopg.Connection, email: str = "rider@example.com") -> str:
    cur = conn.execute(
        "INSERT INTO users (email, claim_consent_at, claim_consent_terms) "
        "VALUES (%s, now(), 'loa-v1') RETURNING id",
        (email,),
    )
    row = cur.fetchone()
    assert row is not None
    return str(row[0])


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin_crs": "MAN",
        "destination_crs": "EUS",
        "travel_date": "2026-08-10",
        "scheduled_departure": _DEP.isoformat(),
        "scheduled_arrival": (_DEP + timedelta(hours=2)).isoformat(),
        "price_pence": 4550,
        "kind": "single",
    }
    body.update(overrides)
    return body


def _count(conn: psycopg.Connection, table: str, user_id: str) -> int:
    # Test-only SQL (S608 is off for tests); the identifier comes from the
    # two literals below, never from input.
    assert table in ("journeys", "tickets")
    row = conn.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s", (user_id,)).fetchone()
    assert row is not None
    return row[0]


def _single_error(resp: Any) -> dict[str, Any]:
    """The one entry of a 422 body — asserting on it pins WHICH guard fired,
    so an unrelated validation failure cannot keep the test green."""
    detail = resp.json()["detail"]
    assert len(detail) == 1, detail
    return detail[0]


@pytest.fixture
def client(conn: psycopg.Connection) -> Iterator[TestClient]:
    app = create_app()

    def _rollback_conn() -> Iterator[psycopg.Connection]:
        # A savepoint per request: a request that dies mid-transaction (409
        # duplicate, 404 on the users FK) must not poison the connection for
        # the next request in the same test. The fixture rolls back the outer
        # transaction afterwards, so nothing ever commits.
        with conn.transaction():
            yield conn

    # Pin the outer transaction open BEFORE any request runs: psycopg only
    # issues BEGIN on first use, and conn.transaction() on an idle connection
    # opens a real top-level transaction whose exit would COMMIT — the
    # "nothing ever commits" comment above is only true once this has run.
    conn.execute("SELECT 1")

    app.dependency_overrides[get_conn] = _rollback_conn
    # Deliberately no `with`: entering the client would run the lifespan and
    # open the real pool, which these tests must never touch.
    yield TestClient(app)


class TestHealth:
    def test_healthz(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestCreateJourney:
    def test_create_round_trip_matches_db_row(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        uid = _mk_user(conn)
        resp = client.post("/journeys", json=_payload(), headers={"X-User-Id": uid})
        assert resp.status_code == 201, resp.text
        body = resp.json()

        row = conn.execute(
            "SELECT id, ticket_id, origin_crs, destination_crs, travel_date, "
            "scheduled_departure, scheduled_arrival, status "
            "FROM journeys WHERE id = %s",
            (body["id"],),
        ).fetchone()
        assert row is not None
        jid, ticket_id, origin, dest, travel_date, dep, arr, status = row
        assert str(jid) == body["id"]
        assert str(ticket_id) == body["ticket_id"]
        assert (origin, dest) == (body["origin_crs"], body["destination_crs"]) == ("MAN", "EUS")
        assert str(travel_date) == body["travel_date"] == "2026-08-10"
        assert dep == datetime.fromisoformat(body["scheduled_departure"]) == _DEP
        assert arr == datetime.fromisoformat(body["scheduled_arrival"]) == _DEP + timedelta(hours=2)
        assert status == body["status"] == "pending"

        # The ticket was created in the same transaction, priced in pence.
        ticket = conn.execute(
            "SELECT price_pence, kind, source FROM tickets WHERE id = %s", (ticket_id,)
        ).fetchone()
        assert ticket == (4550, "single", "manual")

    def test_duplicate_add_is_a_conflict(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        uid = _mk_user(conn)
        headers = {"X-User-Id": uid}
        assert client.post("/journeys", json=_payload(), headers=headers).status_code == 201
        resp = client.post("/journeys", json=_payload(), headers=headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "journey already added"
        # The rejected add minted nothing — no second journey, no orphan ticket.
        assert _count(conn, "journeys", uid) == 1
        assert _count(conn, "tickets", uid) == 1

    def test_same_leg_later_departure_is_a_second_journey(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """Same leg, same day, different departure = two genuine trips (the
        08:14 out and the 18:14 back on a different route happen; so does
        twice-daily travel). 0005 scopes uniqueness to allow this and 0009
        keys on scheduled_departure to keep allowing it."""
        headers = {"X-User-Id": _mk_user(conn)}
        assert client.post("/journeys", json=_payload(), headers=headers).status_code == 201
        evening = _DEP + timedelta(hours=10)
        second = _payload(
            scheduled_departure=evening.isoformat(),
            scheduled_arrival=(evening + timedelta(hours=2)).isoformat(),
        )
        assert client.post("/journeys", json=second, headers=headers).status_code == 201

    def test_unknown_user_is_404_not_500(self, client: TestClient) -> None:
        resp = client.post("/journeys", json=_payload(), headers={"X-User-Id": str(uuid4())})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "unknown user"

    def test_naive_datetime_is_rejected(self, client: TestClient, conn: psycopg.Connection) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        bad = _payload(scheduled_departure="2026-08-10T08:14:00")  # no offset
        resp = client.post("/journeys", json=bad, headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["loc"] == ["body", "scheduled_departure"]
        assert err["type"] == "timezone_aware"

    def test_naive_arrival_is_rejected(self, client: TestClient, conn: psycopg.Connection) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        bad = _payload(scheduled_arrival="2026-08-10T10:14:00")  # no offset
        resp = client.post("/journeys", json=bad, headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["loc"] == ["body", "scheduled_arrival"]
        assert err["type"] == "timezone_aware"

    def test_bad_crs_is_rejected(self, client: TestClient, conn: psycopg.Connection) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        bad = _payload(origin_crs="Manchester")
        resp = client.post("/journeys", json=bad, headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["loc"] == ["body", "origin_crs"]
        assert err["type"] == "string_pattern_mismatch"

    def test_arrival_before_departure_is_rejected(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        bad = _payload(scheduled_arrival=(_DEP - timedelta(minutes=5)).isoformat())
        resp = client.post("/journeys", json=bad, headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["type"] == "value_error"
        assert "scheduled_arrival must be after scheduled_departure" in err["msg"]

    def test_arrival_equal_to_departure_is_rejected(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        # The boundary case: the validator is `<=`, matching the DB's strict `>`.
        headers = {"X-User-Id": _mk_user(conn)}
        bad = _payload(scheduled_arrival=_DEP.isoformat())
        resp = client.post("/journeys", json=bad, headers=headers)
        assert resp.status_code == 422
        assert _single_error(resp)["type"] == "value_error"

    def test_price_above_int4_range_is_422_not_500(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """Python ints are unbounded; tickets.price_pence is int4. Without the
        schema ceiling this reached the INSERT and surfaced as a driver range
        error — a 500 for plainly invalid input."""
        headers = {"X-User-Id": _mk_user(conn)}
        resp = client.post("/journeys", json=_payload(price_pence=3_000_000_000), headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["loc"] == ["body", "price_pence"]
        assert err["type"] == "less_than_equal"

    def test_free_ticket_is_rejected(self, client: TestClient, conn: psycopg.Connection) -> None:
        # The API is stricter than the DB (> 0 vs >= 0): a manual add with no
        # price is a data-entry error.
        headers = {"X-User-Id": _mk_user(conn)}
        resp = client.post("/journeys", json=_payload(price_pence=0), headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["loc"] == ["body", "price_pence"]
        assert err["type"] == "greater_than"

    def test_unknown_kind_is_rejected(self, client: TestClient, conn: psycopg.Connection) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        resp = client.post("/journeys", json=_payload(kind="flex"), headers=headers)
        assert resp.status_code == 422
        err = _single_error(resp)
        assert err["loc"] == ["body", "kind"]
        assert err["type"] == "literal_error"

    def test_missing_user_header_is_401(self, client: TestClient) -> None:
        resp = client.post("/journeys", json=_payload())
        assert resp.status_code == 401
        assert resp.json()["detail"] == "missing X-User-Id header"

    def test_malformed_user_header_is_401(self, client: TestClient) -> None:
        resp = client.post("/journeys", json=_payload(), headers={"X-User-Id": "not-a-uuid"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "malformed X-User-Id header"


class TestReadJourneys:
    def test_list_orders_newest_travel_date_first(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        for day in ("2026-08-10", "2026-08-14", "2026-08-12"):  # deliberately unordered
            dep = datetime.fromisoformat(f"{day}T08:00:00+00:00")
            body = _payload(
                travel_date=day,
                scheduled_departure=dep.isoformat(),
                scheduled_arrival=(dep + timedelta(hours=2)).isoformat(),
            )
            assert client.post("/journeys", json=body, headers=headers).status_code == 201

        resp = client.get("/journeys", headers=headers)
        assert resp.status_code == 200
        page = resp.json()
        assert page["count"] == 3
        assert page["limit"] == 50
        assert [item["travel_date"] for item in page["items"]] == [
            "2026-08-14",
            "2026-08-12",
            "2026-08-10",
        ]

    def test_list_is_scoped_to_the_requesting_user(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """Both users have rows, and each list returns exactly its own —
        dropping WHERE user_id from the list SQL must fail here, not ship."""
        owner = {"X-User-Id": _mk_user(conn, "owner@example.com")}
        snoop = {"X-User-Id": _mk_user(conn, "snoop@example.com")}
        owner_id = client.post("/journeys", json=_payload(), headers=owner).json()["id"]
        snoop_id = client.post("/journeys", json=_payload(), headers=snoop).json()["id"]

        owner_page = client.get("/journeys", headers=owner).json()
        snoop_page = client.get("/journeys", headers=snoop).json()
        assert owner_page["count"] == 1
        assert [item["id"] for item in owner_page["items"]] == [owner_id]
        assert snoop_page["count"] == 1
        assert [item["id"] for item in snoop_page["items"]] == [snoop_id]

    def test_limit_bounds_and_truncation(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        for day in ("2026-08-10", "2026-08-14"):
            dep = datetime.fromisoformat(f"{day}T08:00:00+00:00")
            body = _payload(
                travel_date=day,
                scheduled_departure=dep.isoformat(),
                scheduled_arrival=(dep + timedelta(hours=2)).isoformat(),
            )
            assert client.post("/journeys", json=body, headers=headers).status_code == 201

        assert client.get("/journeys?limit=0", headers=headers).status_code == 422
        assert client.get("/journeys?limit=201", headers=headers).status_code == 422
        page = client.get("/journeys?limit=1", headers=headers).json()
        assert page["limit"] == 1
        assert page["count"] == 1
        assert page["items"][0]["travel_date"] == "2026-08-14"  # newest survives the cut

    def test_list_for_unknown_user_is_empty_200(self, client: TestClient) -> None:
        """Pinned as intentional: listing never checks user existence, so the
        stub auth header's unknown id reads as "no journeys yet" while POST
        answers 404 (its FK proves nonexistence for free). A client cannot use
        this endpoint to distinguish "no user" from "no journeys"; the real
        identity module replaces the contract wholesale."""
        resp = client.get("/journeys", headers={"X-User-Id": str(uuid4())})
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "count": 0, "limit": 50}

    def test_get_by_id(self, client: TestClient, conn: psycopg.Connection) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        created = client.post("/journeys", json=_payload(), headers=headers).json()
        resp = client.get(f"/journeys/{created['id']}", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == created

    def test_get_absent_id_is_404(self, client: TestClient, conn: psycopg.Connection) -> None:
        headers = {"X-User-Id": _mk_user(conn)}
        resp = client.get(f"/journeys/{uuid4()}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "journey not found"

    def test_get_someone_elses_journey_is_404(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        owner = {"X-User-Id": _mk_user(conn, "owner@example.com")}
        snoop = {"X-User-Id": _mk_user(conn, "snoop@example.com")}
        created = client.post("/journeys", json=_payload(), headers=owner).json()
        # The owner sees it; the other user gets the same 404 as for a
        # nonexistent id — existence must not leak.
        assert client.get(f"/journeys/{created['id']}", headers=owner).status_code == 200
        resp = client.get(f"/journeys/{created['id']}", headers=snoop)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "journey not found"


class TestProductionTransactionPath:
    """No overrides: lifespan, real pool, TransactionMiddleware, real COMMIT.

    Everything else in this file substitutes exactly the component that
    persists data in production; this test exists so a broken
    `db.transaction()`, pool setup or middleware commit fails the suite
    instead of shipping. It commits for real, so it cleans up via the users
    CASCADE as the pool fixture requires.
    """

    def test_post_commits_before_the_response_is_trusted(
        self, pool: None, migrated_database: str
    ) -> None:
        with psycopg.connect(migrated_database) as setup:
            uid = _mk_user(setup, "commit-proof@example.com")
            setup.commit()
        try:
            with TestClient(create_app()) as client:
                resp = client.post("/journeys", json=_payload(), headers={"X-User-Id": uid})
                assert resp.status_code == 201, resp.text
                jid = resp.json()["id"]
                # The 409 path rolls back rather than committing half a request.
                dup = client.post("/journeys", json=_payload(), headers={"X-User-Id": uid})
                assert dup.status_code == 409

            # Visible from a second, fresh connection => a real COMMIT happened.
            with psycopg.connect(migrated_database) as fresh:
                rows = fresh.execute(
                    "SELECT id FROM journeys WHERE user_id = %s", (uid,)
                ).fetchall()
                assert [str(r[0]) for r in rows] == [jid]
        finally:
            with psycopg.connect(migrated_database) as cleanup:
                cleanup.execute("DELETE FROM users WHERE id = %s", (uid,))
                cleanup.commit()
