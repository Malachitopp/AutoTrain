"""API tests for the auth vertical slice: the magic-link endpoints, the
bearer-token gate they put in front of every other route, and the CORS gate
a browser frontend has to pass first.

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

from autotrain.api import app as app_module
from autotrain.api.app import create_app
from autotrain.api.deps import get_conn
from autotrain.api.routers import auth as auth_router
from autotrain.core.config import get_settings
from conftest import TEST_APP_BASE_URL, TEST_JWT_SECRET, auth_header, mk_user

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

    def test_link_points_at_the_frontend_login_route(
        self, client: TestClient, sender: _RecordingEmailSender
    ) -> None:
        """The email body IS the link, and its shape — <app base url>/login#
        token=<token> — is the contract the frontend's login page implements.
        The token rides in the fragment, which a browser never sends to any
        server, so it stays out of the frontend host's request logs."""
        client.post("/auth/login/request", json={"email": _EMAIL})
        token = _token_from(sender)
        assert sender.sent[0][2] == f"{TEST_APP_BASE_URL}/login#token={token}"

    @pytest.mark.parametrize("unset", [None, ""], ids=["absent", "blank"])
    def test_unconfigured_app_base_url_is_503(
        self,
        client: TestClient,
        sender: _RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
        unset: str | None,
    ) -> None:
        # The router looks get_settings up at call time; hand it a copy with
        # the knob unset. No link can be built, so no email is sent either.
        # Blank is what a deployment template produces for a missing value;
        # it must refuse the same way, not email a host-less link.
        monkeypatch.setattr(
            auth_router,
            "get_settings",
            lambda: get_settings().model_copy(update={"app_base_url": unset}),
        )
        resp = client.post("/auth/login/request", json={"email": _EMAIL})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "no app base URL configured"
        assert sender.sent == []

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

        headers = {"Authorization": f"Bearer {session}"}
        journeys = client.get("/journeys", headers=headers)
        assert journeys.status_code == 200
        assert journeys.json() == {"items": [], "count": 0, "limit": 50}

        # And the session knows who it is: a first-login account, not yet
        # consented to auto-filing.
        me = client.get("/auth/me", headers=headers).json()
        assert me["email"] == _EMAIL
        assert me["claim_consent_at"] is None

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

    def test_erased_account_is_401(self, client: TestClient, conn: psycopg.Connection) -> None:
        """A session outlives GDPR erasure by up to 30 days. The gate checks
        the account is still live on every request, so the ghost is refused
        everywhere — not only where a handler happens to load the user."""
        user_id = mk_user(conn, "gone@example.com")
        conn.execute("UPDATE users SET email = NULL, deleted_at = now() WHERE id = %s", (user_id,))
        resp = client.get("/journeys", headers=auth_header(user_id))
        assert resp.status_code == 401
        # Indistinguishable from any other bad credential, on purpose.
        assert resp.json()["detail"] == "invalid token"

    def test_never_created_account_is_401(self, client: TestClient) -> None:
        # Validly signed, but the subject was never a row: same refusal.
        resp = client.get("/journeys", headers=auth_header(uuid4()))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid token"

    def test_auth_endpoints_themselves_need_no_session(self, client: TestClient) -> None:
        # The two doors into the building are outside the gate — a fresh
        # client with no token can always start a login.
        resp = client.post("/auth/login/verify", json={"token": "whatever"})
        assert resp.status_code == 401  # judged on the token's merits, not gated
        resp = client.get("/journeys")
        assert resp.status_code == 401  # everything else IS gated
        assert auth_header(uuid4())["Authorization"].startswith("Bearer ")


class TestMe:
    def test_returns_the_verified_users_profile(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        user_id = mk_user(conn, "me@example.com")
        # A non-UTC session zone (no DST, so the offset is never zero by
        # luck): without UserOut's serializer the wire would read +09:00.
        conn.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
        resp = client.get("/auth/me", headers=auth_header(user_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(user_id)
        assert body["email"] == "me@example.com"
        assert body["claim_consent_at"] is not None  # mk_user consents
        # The API speaks UTC only, whatever zone the database session is in.
        for stamp in (body["created_at"], body["claim_consent_at"]):
            assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)
        # Only the profile shape crosses the wire — never the table.
        assert set(body) == {"id", "email", "claim_consent_at", "created_at"}

    def test_erased_user_is_401(self, client: TestClient, conn: psycopg.Connection) -> None:
        """The bearer gate refuses an erased account before this route runs,
        so its own 404 branch is now defence in depth the wire never shows.
        Pinned here so a gate regression surfaces on /auth/me too."""
        user_id = mk_user(conn, "gone@example.com")
        conn.execute("UPDATE users SET email = NULL, deleted_at = now() WHERE id = %s", (user_id,))
        resp = client.get("/auth/me", headers=auth_header(user_id))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid token"

    def test_requires_a_session(self, client: TestClient) -> None:
        assert client.get("/auth/me").status_code == 401


# What a browser sends before a cross-origin GET that carries Authorization.
_PREFLIGHT = {
    "Access-Control-Request-Method": "GET",
    "Access-Control-Request-Headers": "authorization",
}


class TestCors:
    """The browser's gate. Origins come from config and default to none; the
    list is pinned per test because nothing else in the suite sends an Origin
    header. Settings are read inside create_app(), so the seam is the app
    module's get_settings, patched before the client is built."""

    @staticmethod
    def _client(
        conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, origins: list[str]
    ) -> TestClient:
        monkeypatch.setattr(
            app_module,
            "get_settings",
            lambda: get_settings().model_copy(update={"cors_origins": origins}),
        )
        return _client_for(conn)

    def test_listed_origin_may_send_a_bearer_token(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(conn, monkeypatch, [TEST_APP_BASE_URL])
        # The preflight: the browser asks whether it may send Authorization.
        resp = client.options("/auth/me", headers={"Origin": TEST_APP_BASE_URL, **_PREFLIGHT})
        assert resp.status_code == 200, resp.text
        assert resp.headers["access-control-allow-origin"] == TEST_APP_BASE_URL
        assert "authorization" in resp.headers["access-control-allow-headers"].lower()
        # The real request carries the header back even on a 401 — without
        # it the browser hides the response from the page entirely.
        denied = client.get("/auth/me", headers={"Origin": TEST_APP_BASE_URL})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == TEST_APP_BASE_URL

    def test_unlisted_origin_is_refused(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(conn, monkeypatch, [TEST_APP_BASE_URL])
        resp = client.options("/auth/me", headers={"Origin": "http://evil.test", **_PREFLIGHT})
        assert resp.status_code == 400
        assert "access-control-allow-origin" not in resp.headers
        # Simple requests are still served (CORS is the browser's rule, not
        # ours), but without the header the browser withholds the response.
        simple = client.get("/healthz", headers={"Origin": "http://evil.test"})
        assert simple.status_code == 200
        assert "access-control-allow-origin" not in simple.headers

    def test_default_is_closed(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(conn, monkeypatch, [])
        resp = client.options("/auth/me", headers={"Origin": TEST_APP_BASE_URL, **_PREFLIGHT})
        assert resp.status_code == 400
        assert "access-control-allow-origin" not in resp.headers
