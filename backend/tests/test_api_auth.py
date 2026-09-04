"""API tests for the auth vertical slice: the magic-link endpoints and the
bearer-token gate they put in front of every other route.

Same harness as test_api_journeys (get_conn overridden to the rollback conn),
with one more substitution at the same kind of seam: the router's email-sender
builder is patched to hand back a recording double, so each test's "inbox" is
a list. The unconfigured-transport test deliberately skips that patch — the
suite's settings leave email_sender at 'none', so the 503 path is the real one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from autotrain.api.app import create_app
from autotrain.api.deps import get_conn
from autotrain.api.routers import auth as auth_router
from conftest import TEST_JWT_SECRET, auth_header, mk_user

_EMAIL = "rider@example.com"


class _RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


def _client_for(conn: psycopg.Connection) -> TestClient:
    """The journeys-suite client: real app, rollback conn, pool never opened."""
    app = create_app()

    def _rollback_conn() -> Iterator[psycopg.Connection]:
        with conn.transaction():
            yield conn

    conn.execute("SELECT 1")  # pin the outer transaction open first
    app.dependency_overrides[get_conn] = _rollback_conn
    return TestClient(app)


@pytest.fixture
def sender() -> _RecordingEmailSender:
    return _RecordingEmailSender()


@pytest.fixture
def client(
    conn: psycopg.Connection, sender: _RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    # The router looks _email_sender up at call time, so patching the module
    # attribute swaps the transport for exactly this test — the same seam the
    # entrypoint uses, exercised the same way.
    monkeypatch.setattr(auth_router, "_email_sender", lambda: sender)
    return _client_for(conn)


def _token_from(sender: _RecordingEmailSender) -> str:
    assert len(sender.sent) == 1
    return sender.sent[0][2].split("token=")[1]


class TestRequestLink:
    def test_delivers_the_email_and_says_nothing(
        self, client: TestClient, sender: _RecordingEmailSender
    ) -> None:
        resp = client.post("/auth/login/request", json={"email": _EMAIL})
        assert resp.status_code == 204
        assert resp.content == b""  # the token travels through the email only
        to, _subject, body = sender.sent[0]
        assert to == _EMAIL
        assert "token=" in body

    def test_known_and_unknown_emails_are_indistinguishable(
        self, client: TestClient, sender: _RecordingEmailSender, conn: psycopg.Connection
    ) -> None:
        """User enumeration, pinned at the HTTP layer: same status, same
        (empty) body, whether or not the address has an account."""
        mk_user(conn, "known@example.com")
        known = client.post("/auth/login/request", json={"email": "known@example.com"})
        unknown = client.post("/auth/login/request", json={"email": "stranger@example.com"})
        assert known.status_code == unknown.status_code == 204
        assert known.content == unknown.content == b""
        assert len(sender.sent) == 2

    def test_unconfigured_email_transport_is_503(self, conn: psycopg.Connection) -> None:
        # No sender patch: settings leave email_sender at 'none', and the
        # refusal is scoped to this endpoint, not the whole app.
        client = _client_for(conn)
        resp = client.post("/auth/login/request", json={"email": _EMAIL})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "no email sender configured"
        assert client.get("/healthz").status_code == 200


class TestVerify:
    def test_full_login_flow_end_to_end(
        self, client: TestClient, sender: _RecordingEmailSender
    ) -> None:
        """The whole story in one test: request a link, read the 'inbox',
        exchange the token for a session, and use the session on a protected
        route. No stub anywhere — this is the production auth path."""
        assert client.post("/auth/login/request", json={"email": _EMAIL}).status_code == 204
        token = _token_from(sender)

        verified = client.post("/auth/login/verify", json={"token": token})
        assert verified.status_code == 200, verified.text
        session = verified.json()["access_token"]

        journeys = client.get("/journeys", headers={"Authorization": f"Bearer {session}"})
        assert journeys.status_code == 200
        assert journeys.json() == {"items": [], "count": 0, "limit": 50}

    def test_token_is_single_use(self, client: TestClient, sender: _RecordingEmailSender) -> None:
        client.post("/auth/login/request", json={"email": _EMAIL})
        token = _token_from(sender)
        assert client.post("/auth/login/verify", json={"token": token}).status_code == 200
        replay = client.post("/auth/login/verify", json={"token": token})
        assert replay.status_code == 401
        assert replay.json()["detail"] == "invalid token"

    def test_garbage_token_is_401(self, client: TestClient) -> None:
        resp = client.post("/auth/login/verify", json={"token": "never-issued"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid token"


class TestSessionGate:
    def test_expired_session_is_401(self, client: TestClient) -> None:
        stale = jwt.encode(
            {"sub": str(uuid4()), "exp": datetime.now(UTC) - timedelta(minutes=1)},
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        resp = client.get("/journeys", headers={"Authorization": f"Bearer {stale}"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid token"

    def test_forged_signature_is_401(self, client: TestClient) -> None:
        forged = jwt.encode(
            {"sub": str(uuid4()), "exp": datetime.now(UTC) + timedelta(days=30)},
            "not-the-real-secret-but-still-32-bytes-long",
            algorithm="HS256",
        )
        resp = client.get("/journeys", headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401

    def test_auth_endpoints_themselves_need_no_session(self, client: TestClient) -> None:
        # The two doors into the building are outside the gate — a fresh
        # client with no token can always start a login.
        resp = client.post("/auth/login/verify", json={"token": "whatever"})
        assert resp.status_code == 401  # judged on the token's merits, not gated
        resp = client.get("/journeys")
        assert resp.status_code == 401  # everything else IS gated
        assert auth_header(uuid4())["Authorization"].startswith("Bearer ")
