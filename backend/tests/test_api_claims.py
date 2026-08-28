"""API tests for the claims and delay-decision read endpoints.

Same harness as test_api_journeys: the client drives HTTP through the real app
with get_conn overridden to the rollback `conn` fixture — routing, schemas,
services, real Postgres, nothing committed. Claim rows are built through the
claims service (the same writer production uses), never with test-side
shortcuts past the state machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from autotrain.api.app import create_app
from autotrain.api.deps import get_conn
from autotrain.modules.claims.service import open_claim, run_claim_sweep, transition
from conftest import mk_user, scalar

_DEP = datetime(2026, 8, 10, 8, 14, tzinfo=UTC)
ENTITLEMENT = 2275


# Same fixture as test_api_journeys (promote to conftest on the fourth copy —
# the house rule recorded there).
@pytest.fixture
def client(conn: psycopg.Connection) -> Iterator[TestClient]:
    app = create_app()

    def _rollback_conn() -> Iterator[psycopg.Connection]:
        # A savepoint per request: a request that dies mid-transaction must
        # not poison the connection for the next request in the same test.
        with conn.transaction():
            yield conn

    # Pin the outer transaction open BEFORE any request runs (psycopg issues
    # BEGIN on first use; without this, savepoint exits would COMMIT).
    conn.execute("SELECT 1")

    app.dependency_overrides[get_conn] = _rollback_conn
    # No `with`: entering the client would run the lifespan and open the pool.
    yield TestClient(app)


# --- Row builders (through the service where a service writer exists) --------


def _mk_operator(conn: psycopg.Connection, atoc: str = "QQ") -> UUID:
    return scalar(
        conn.execute(
            "INSERT INTO operators (atoc_code, name, min_delay_minutes, claim_window_days) "
            "VALUES (%s, 'Test Railways', 15, 28) RETURNING id",
            (atoc,),
        )
    )


def _mk_entitled_journey(
    conn: psycopg.Connection,
    user_id: UUID,
    operator_id: UUID,
    *,
    departure: datetime = _DEP,
    origin: str = "MAN",
    entitlement_pence: int = ENTITLEMENT,
) -> tuple[UUID, UUID]:
    """(journey_id, detection_id): an assessed journey plus its frozen
    detection — the state the ingestor leaves behind."""
    ticket_id = scalar(
        conn.execute(
            "INSERT INTO tickets (user_id, kind, price_pence, source) "
            "VALUES (%s, 'single', 4550, 'manual') RETURNING id",
            (user_id,),
        )
    )
    journey_id = scalar(
        conn.execute(
            "INSERT INTO journeys (user_id, ticket_id, operator_id, origin_crs, "
            "destination_crs, travel_date, scheduled_departure, scheduled_arrival, status) "
            "VALUES (%s, %s, %s, %s, 'EUS', %s, %s, %s, 'assessed') RETURNING id",
            (
                user_id,
                ticket_id,
                operator_id,
                origin,
                departure.date(),
                departure,
                departure + timedelta(hours=2),
            ),
        )
    )
    detection_id = scalar(
        conn.execute(
            "INSERT INTO delay_detections (journey_id, actual_arrival, delay_minutes, "
            "source, band_percent, entitlement_pence) "
            "VALUES (%s, %s, 45, 'hsp', 50, %s) RETURNING id",
            (journey_id, departure + timedelta(hours=2, minutes=45), entitlement_pence),
        )
    )
    return journey_id, detection_id


def _mk_claim(
    conn: psycopg.Connection, user_id: UUID, operator_id: UUID, **kw
) -> tuple[UUID, UUID]:
    """(claim_id, journey_id) via the real sweep — production's own writer."""
    journey_id, _ = _mk_entitled_journey(conn, user_id, operator_id, **kw)
    run_claim_sweep(conn)
    claim_id = scalar(conn.execute("SELECT id FROM claims WHERE journey_id = %s", (journey_id,)))
    return claim_id, journey_id


def _hdr(user_id: UUID) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


# --- /claims ------------------------------------------------------------------


class TestListClaims:
    def test_round_trip_matches_db_row(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        claim_id, journey_id = _mk_claim(conn, user_id, _mk_operator(conn))

        resp = client.get("/claims", headers=_hdr(user_id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1 and body["limit"] == 50
        (item,) = body["items"]
        assert item["id"] == str(claim_id)
        assert item["journey_id"] == str(journey_id)
        assert item["amount_pence"] == ENTITLEMENT
        assert item["status"] == "draft"
        assert item["file_by"] == (_DEP.date() + timedelta(days=28)).isoformat()
        assert item["submitted_at"] is None and item["resolved_at"] is None
        # Internal fields stay off the wire.
        assert "submission_token" not in item
        assert "detection_id" not in item
        assert "user_id" not in item

    def test_scoped_to_the_requesting_user(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        owner = mk_user(conn, "owner@example.com")
        stranger = mk_user(conn, "stranger@example.com")
        _mk_claim(conn, owner, _mk_operator(conn))

        resp = client.get("/claims", headers=_hdr(stranger))

        assert resp.status_code == 200
        assert resp.json() == {"items": [], "count": 0, "limit": 50}

    def test_newest_first(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        operator_id = _mk_operator(conn)
        for hours, origin in ((0, "MAN"), (3, "LDS")):
            _mk_entitled_journey(
                conn, user_id, operator_id, departure=_DEP + timedelta(hours=hours), origin=origin
            )
        run_claim_sweep(conn)

        resp = client.get("/claims", headers=_hdr(user_id))

        created = [item["created_at"] for item in resp.json()["items"]]
        assert created == sorted(created, reverse=True)

    def test_limit_out_of_bounds_is_422(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        assert client.get("/claims?limit=0", headers=_hdr(user_id)).status_code == 422
        assert client.get("/claims?limit=201", headers=_hdr(user_id)).status_code == 422

    def test_missing_user_header_is_401(self, client: TestClient) -> None:
        assert client.get("/claims").status_code == 401


class TestGetClaim:
    def test_get_by_id(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        claim_id, _ = _mk_claim(conn, user_id, _mk_operator(conn))

        resp = client.get(f"/claims/{claim_id}", headers=_hdr(user_id))

        assert resp.status_code == 200
        assert resp.json()["id"] == str(claim_id)

    def test_absent_id_is_404(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        assert client.get(f"/claims/{uuid4()}", headers=_hdr(user_id)).status_code == 404

    def test_someone_elses_claim_is_404(self, client: TestClient, conn: psycopg.Connection) -> None:
        owner = mk_user(conn, "owner@example.com")
        stranger = mk_user(conn, "stranger@example.com")
        claim_id, _ = _mk_claim(conn, owner, _mk_operator(conn))

        resp = client.get(f"/claims/{claim_id}", headers=_hdr(stranger))

        # Indistinguishable from absent: existence never leaks.
        assert resp.status_code == 404
        assert resp.json() == client.get(f"/claims/{uuid4()}", headers=_hdr(stranger)).json()


class TestClaimEvents:
    def test_full_trail_in_order(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        claim_id, _ = _mk_claim(conn, user_id, _mk_operator(conn))
        assert transition(conn, claim_id, "ready", detail="auto-file enabled")

        resp = client.get(f"/claims/{claim_id}/events", headers=_hdr(user_id))

        assert resp.status_code == 200
        events = resp.json()
        assert [(e["from_status"], e["to_status"]) for e in events] == [
            (None, "draft"),
            ("draft", "ready"),
        ]
        assert events[1]["detail"] == "auto-file enabled"

    def test_someone_elses_events_are_404(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """THE ownership test for this router: claim_history itself is
        unscoped, so this 404 proves the router's ownership gate exists."""
        owner = mk_user(conn, "owner@example.com")
        stranger = mk_user(conn, "stranger@example.com")
        claim_id, _ = _mk_claim(conn, owner, _mk_operator(conn))

        assert client.get(f"/claims/{claim_id}/events", headers=_hdr(stranger)).status_code == 404


# --- /journeys/{id}/decision --------------------------------------------------


class TestJourneyDecision:
    def test_round_trip(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        journey_id, _ = _mk_entitled_journey(conn, user_id, _mk_operator(conn))

        resp = client.get(f"/journeys/{journey_id}/decision", headers=_hdr(user_id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["delay_minutes"] == 45
        assert body["band_percent"] == 50
        assert body["entitlement_pence"] == ENTITLEMENT
        assert body["source"] == "hsp"
        # The API speaks UTC: the serialized instant equals the one inserted.
        assert datetime.fromisoformat(body["actual_arrival"]) == _DEP + timedelta(
            hours=2, minutes=45
        )

    def test_undecided_journey_is_404(self, client: TestClient, conn: psycopg.Connection) -> None:
        """Owned but not yet decided — still awaiting the sweep. The detail
        string differs from the not-found case for the owner's benefit."""
        user_id = mk_user(conn)
        ticket_id = scalar(
            conn.execute(
                "INSERT INTO tickets (user_id, kind, price_pence, source) "
                "VALUES (%s, 'single', 4550, 'manual') RETURNING id",
                (user_id,),
            )
        )
        journey_id = scalar(
            conn.execute(
                "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
                "travel_date, scheduled_departure, scheduled_arrival) "
                "VALUES (%s, %s, 'MAN', 'EUS', %s, %s, %s) RETURNING id",
                (user_id, ticket_id, _DEP.date(), _DEP, _DEP + timedelta(hours=2)),
            )
        )

        resp = client.get(f"/journeys/{journey_id}/decision", headers=_hdr(user_id))

        assert resp.status_code == 404
        assert resp.json()["detail"] == "no delay decision yet"

    def test_someone_elses_journey_is_404(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """A stranger gets 'journey not found' — never 'no delay decision
        yet', which would leak the journey's existence."""
        owner = mk_user(conn, "owner@example.com")
        stranger = mk_user(conn, "stranger@example.com")
        journey_id, _ = _mk_entitled_journey(conn, owner, _mk_operator(conn))

        resp = client.get(f"/journeys/{journey_id}/decision", headers=_hdr(stranger))

        assert resp.status_code == 404
        assert resp.json()["detail"] == "journey not found"

    def test_absent_journey_is_404(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        resp = client.get(f"/journeys/{uuid4()}/decision", headers=_hdr(user_id))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "journey not found"


# --- The service round trip the API leans on ----------------------------------


def test_open_claim_visible_through_api(client: TestClient, conn: psycopg.Connection) -> None:
    """Belt and braces for the seam: a claim opened by open_claim directly
    (the event-bus path of the future) reads back identically to one from the
    sweep."""
    user_id = mk_user(conn)
    operator_id = _mk_operator(conn)
    journey_id, detection_id = _mk_entitled_journey(conn, user_id, operator_id)

    row = conn.execute(
        "SELECT journey_id, entitlement_pence, observed_at FROM delay_detections WHERE id = %s",
        (detection_id,),
    ).fetchone()
    assert row is not None
    from autotrain.modules.claims.service import ClaimContext, UnclaimedDetection

    claim = open_claim(
        conn,
        UnclaimedDetection(
            id=detection_id, journey_id=row[0], entitlement_pence=row[1], observed_at=row[2]
        ),
        ClaimContext(
            journey_id=journey_id,
            user_id=user_id,
            operator_id=operator_id,
            travel_date=_DEP.date(),
            claim_window_days=28,
        ),
    )
    assert claim is not None

    resp = client.get(f"/claims/{claim.id}", headers=_hdr(user_id))
    assert resp.status_code == 200
    assert resp.json()["amount_pence"] == ENTITLEMENT
    assert resp.json()["file_by"] == date(2026, 9, 7).isoformat()  # Aug 10 + 28 days


class TestClaimSummary:
    def test_new_users(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)

        resp = client.get("/claims/summary", headers=_hdr(user_id))

        assert resp.status_code == 200
        assert resp.json() == {"recovered_pence": 0, "pending_pence": 0}

    def test_correct_buckets(self, client: TestClient, conn: psycopg.Connection) -> None:
        user_id = mk_user(conn)
        operator_id = _mk_operator(conn)
        paid_claim, _ = _mk_claim(conn, user_id, operator_id)
        _mk_claim(conn, user_id, operator_id, origin="LDS", entitlement_pence=1000)

        assert transition(conn, paid_claim, "ready")
        assert transition(conn, paid_claim, "submitted")
        assert transition(conn, paid_claim, "paid")

        resp = client.get("/claims/summary", headers=_hdr(user_id))

        assert resp.status_code == 200
        assert resp.json() == {"recovered_pence": 2275, "pending_pence": 1000}

    def test_scoped_to_the_requesting_user(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        owner = mk_user(conn, "owner@example.com")
        stranger = mk_user(conn, "stranger@example.com")
        paid_claim, _ = _mk_claim(conn, owner, _mk_operator(conn))
        assert transition(conn, paid_claim, "ready")
        assert transition(conn, paid_claim, "submitted")
        assert transition(conn, paid_claim, "paid")

        # The owner really has money on the books — so the stranger's zeros
        # below prove scoping, not an accidentally empty database.
        owner_resp = client.get("/claims/summary", headers=_hdr(owner))
        assert owner_resp.json()["recovered_pence"] == ENTITLEMENT

        resp = client.get("/claims/summary", headers=_hdr(stranger))

        assert resp.status_code == 200
        assert resp.json() == {"recovered_pence": 0, "pending_pence": 0}

    def test_summary_route_is_not_shadowed_by_the_claim_id_route(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """Route-order tripwire. FastAPI matches routes in declaration order,
        and "/{claim_id}" happily matches the literal segment "summary" —
        then fails to parse it as a UUID, answering 422. The summary route
        must stay declared ABOVE "/{claim_id}"; this test fails the moment
        anyone tidies the router into an order that breaks that."""
        user_id = mk_user(conn)

        resp = client.get("/claims/summary", headers=_hdr(user_id))

        assert resp.status_code == 200  # 422 here = the route got shadowed again
