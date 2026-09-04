"""Identity service tests: the magic-link lifecycle and session tokens.

Same rules as every other service suite: real Postgres through the rollback
`conn` fixture, and the transport replaced at the sanctioned seam — the email
sender is a recording double, so each test's "inbox" is a list it can read
the token back out of. The session-token tests touch no database at all:
issue/verify is pure signature math, which is rather the point of it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import psycopg

from autotrain.modules.identity import service as identity
from conftest import TEST_APP_BASE_URL, TEST_JWT_SECRET, mk_user, scalar

_EMAIL = "magic-link@example.com"


class _RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


def _request_token(conn: psycopg.Connection, email: str = _EMAIL) -> str:
    """Run request_login and pull the raw token back out of the 'email' —
    the same move a real user makes in their inbox."""
    sender = _RecordingEmailSender()
    identity.request_login(conn, email, sender, app_base_url=TEST_APP_BASE_URL)
    assert len(sender.sent) == 1
    to, _subject, body = sender.sent[0]
    assert to == email
    # The link's shape is a contract with the frontend's /login route.
    assert body.startswith(f"{TEST_APP_BASE_URL}/login?token=")
    return body.split("token=")[1]


# --- The magic-link lifecycle -------------------------------------------------


class TestRequestLogin:
    def test_stores_the_hash_and_never_the_token(self, conn: psycopg.Connection) -> None:
        token = _request_token(conn)
        row = conn.execute(
            "SELECT token_hash, expires_at, used_at FROM login_tokens WHERE email = %s",
            (_EMAIL,),
        ).fetchone()
        assert row is not None
        token_hash, expires_at, used_at = row
        assert token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token_hash != token  # the raw token appears nowhere in the DB
        assert used_at is None
        # The 15-minute policy, with slack for the test's own runtime.
        remaining = expires_at - datetime.now(UTC)
        assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)

    def test_never_reveals_whether_the_email_has_an_account(self, conn: psycopg.Connection) -> None:
        """The enumeration property at the service layer: a known and an
        unknown email produce indistinguishable behaviour — one stored row,
        one sent email, no exception, either way."""
        mk_user(conn, "known@example.com")
        for email in ("known@example.com", "stranger@example.com"):
            sender = _RecordingEmailSender()
            identity.request_login(conn, email, sender, app_base_url=TEST_APP_BASE_URL)
            assert len(sender.sent) == 1
            count = scalar(
                conn.execute("SELECT count(*) FROM login_tokens WHERE email = %s", (email,))
            )
            assert count == 1


class TestVerifyLogin:
    def test_first_login_creates_the_user(self, conn: psycopg.Connection) -> None:
        token = _request_token(conn)
        user_id = identity.verify_login(conn, token)
        assert isinstance(user_id, UUID)

        row = conn.execute(
            "SELECT email, claim_consent_at, claim_consent_terms FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
        # Signup is not consent: the account exists, the authority columns
        # stay empty until the user explicitly grants them in the app.
        assert row == (_EMAIL, None, None)

        used_at = scalar(
            conn.execute("SELECT used_at FROM login_tokens WHERE email = %s", (_EMAIL,))
        )
        assert used_at is not None

    def test_existing_user_logs_into_their_account(self, conn: psycopg.Connection) -> None:
        existing = mk_user(conn, _EMAIL)
        token = _request_token(conn)
        assert identity.verify_login(conn, token) == existing
        # Logged in, not duplicated.
        assert scalar(conn.execute("SELECT count(*) FROM users WHERE email = %s", (_EMAIL,))) == 1

    def test_token_is_single_use(self, conn: psycopg.Connection) -> None:
        token = _request_token(conn)
        assert identity.verify_login(conn, token) is not None
        assert identity.verify_login(conn, token) is None

    def test_unknown_token_is_refused(self, conn: psycopg.Connection) -> None:
        assert identity.verify_login(conn, "never-issued") is None

    def test_expired_token_is_refused(self, conn: psycopg.Connection) -> None:
        token = _request_token(conn)
        conn.execute(
            "UPDATE login_tokens SET expires_at = now() - interval '1 minute' WHERE email = %s",
            (_EMAIL,),
        )
        assert identity.verify_login(conn, token) is None
        # And expiry is final: the token was not spent, but it can never win.
        used_at = scalar(
            conn.execute("SELECT used_at FROM login_tokens WHERE email = %s", (_EMAIL,))
        )
        assert used_at is None


# --- Session tokens (no database: pure signature math) ------------------------


class TestSessionTokens:
    def test_round_trip(self) -> None:
        user_id = uuid4()
        token = identity.issue_session_token(user_id, secret=TEST_JWT_SECRET)
        assert identity.session_user(token, secret=TEST_JWT_SECRET) == user_id

    def test_tampered_token_is_refused(self) -> None:
        token = identity.issue_session_token(uuid4(), secret=TEST_JWT_SECRET)
        assert identity.session_user(token[:-2], secret=TEST_JWT_SECRET) is None

    def test_wrong_secret_is_refused(self) -> None:
        token = identity.issue_session_token(uuid4(), secret=TEST_JWT_SECRET)
        assert identity.session_user(token, secret="a-different-secret-padded-to-32-bytes") is None

    def test_expired_session_is_refused(self) -> None:
        stale = jwt.encode(
            {"sub": str(uuid4()), "exp": datetime.now(UTC) - timedelta(minutes=1)},
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        assert identity.session_user(stale, secret=TEST_JWT_SECRET) is None

    def test_valid_signature_without_a_subject_is_refused(self) -> None:
        # Correctly signed, correctly unexpired, but naming nobody: the
        # KeyError arm of the verifier, which must refuse rather than crash.
        anonymous = jwt.encode(
            {"exp": datetime.now(UTC) + timedelta(days=1)},
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        assert identity.session_user(anonymous, secret=TEST_JWT_SECRET) is None
