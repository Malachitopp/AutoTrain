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
import pytest

from autotrain.modules.identity import service as identity
from conftest import TEST_APP_BASE_URL, TEST_JWT_SECRET, mk_user, scalar

_EMAIL = "magic-link@example.com"


class _RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


def link_in(body: str) -> str:
    """The one line of the email that is the link.

    Exactly one, and alone: mail clients linkify a URL that owns its line,
    and wrap — and so break — one buried in a sentence. That the link stands
    by itself is part of the message's contract with the inbox, so pulling
    it out this way asserts it on every test that reads a token.
    """
    (link,) = [line for line in body.splitlines() if line.startswith(TEST_APP_BASE_URL)]
    return link


def _request_token(conn: psycopg.Connection, email: str = _EMAIL) -> str:
    """Run request_login and pull the raw token back out of the 'email' —
    the same move a real user makes in their inbox."""
    sender = _RecordingEmailSender()
    identity.request_login(conn, email, sender, app_base_url=TEST_APP_BASE_URL)
    assert len(sender.sent) == 1
    to, _subject, body = sender.sent[0]
    assert to == email
    # The link's shape is a contract with the frontend's /login route.
    link = link_in(body)
    assert link.startswith(f"{TEST_APP_BASE_URL}/login#token=")
    return link.split("token=")[1]


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

    def test_the_email_is_a_message_and_not_a_bare_link(self, conn: psycopg.Connection) -> None:
        """A body that is nothing but a long URL is one of the oldest
        phishing shapes there is, and spam filters score it that way — for
        the one email in this product that must arrive. So the message says
        what it is, how long the link lasts, and what to do if the reader
        did not ask for it; and the expiry it promises is read from the same
        constant the database row is stamped from, so the two cannot drift.
        """
        sender = _RecordingEmailSender()
        identity.request_login(conn, _EMAIL, sender, app_base_url=TEST_APP_BASE_URL)
        _to, subject, body = sender.sent[0]

        assert "AutoTrain" in subject
        assert body.splitlines()[0].endswith(":")  # the link is introduced
        assert "15 minutes" in body  # the row's own expiry, asserted above
        assert "did not ask" in body
        # The token appears once and only inside the link.
        token = link_in(body).split("token=")[1]
        assert body.count(token) == 1

    def test_a_failed_send_leaves_no_token_behind(self, conn: psycopg.Connection) -> None:
        """Why the send sits inside the caller's transaction rather than
        after it. A token whose email never went out is a row nobody can
        ever spend and an attacker can still guess at, so the delivery
        failure has to unwind the INSERT with it — which only works while
        both are one unit of work."""

        class _Failing:
            def send_email(self, *, to: str, subject: str, body: str) -> None:
                raise identity.EmailDeliveryError("the provider refused the message")

        email = "undeliverable@example.com"
        with pytest.raises(identity.EmailDeliveryError), conn.transaction():
            identity.request_login(conn, email, _Failing(), app_base_url=TEST_APP_BASE_URL)

        count = scalar(conn.execute("SELECT count(*) FROM login_tokens WHERE email = %s", (email,)))
        assert count == 0

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


class TestUserIsLive:
    """The bearer gate's per-request question, answered at the service seam."""

    def test_live_account(self, conn: psycopg.Connection) -> None:
        assert identity.user_is_live(conn, mk_user(conn, _EMAIL)) is True

    def test_erased_account_reads_as_gone(self, conn: psycopg.Connection) -> None:
        # GDPR erasure keeps the row (claims must stay auditable) but stamps
        # deleted_at; the gate must see the stamp, not the surviving row.
        user_id = mk_user(conn, _EMAIL)
        conn.execute("UPDATE users SET email = NULL, deleted_at = now() WHERE id = %s", (user_id,))
        assert identity.user_is_live(conn, user_id) is False

    def test_unknown_account(self, conn: psycopg.Connection) -> None:
        # A bool either way: EXISTS never yields None for a missing row.
        assert identity.user_is_live(conn, uuid4()) is False


# --- Session tokens (no database: pure signature math) ------------------------


class TestSessionTokens:
    def test_round_trip(self) -> None:
        user_id = uuid4()
        token = identity.issue_session_token(user_id, secret=TEST_JWT_SECRET)
        claims = identity.session_user(token, secret=TEST_JWT_SECRET)
        assert claims is not None
        assert claims.user_id == user_id
        # iat is whole seconds and within a breath of now.
        assert abs((datetime.now(UTC) - claims.issued_at).total_seconds()) < 5

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


# --- The session cutoff (0014) ----------------------------------------------


class TestSessionCutoff:
    def test_sign_out_everywhere_refuses_earlier_sessions_and_accepts_later_ones(
        self, conn: psycopg.Connection
    ) -> None:
        user_id = mk_user(conn, _EMAIL)
        before = identity.session_user(
            identity.issue_session_token(
                user_id, secret=TEST_JWT_SECRET, now=datetime.now(UTC) - timedelta(seconds=5)
            ),
            secret=TEST_JWT_SECRET,
        )
        assert before is not None
        assert identity.session_is_live(conn, user_id=user_id, issued_at=before.issued_at)

        assert identity.revoke_sessions(conn, user_id) is True

        assert not identity.session_is_live(conn, user_id=user_id, issued_at=before.issued_at)
        after = identity.session_user(
            identity.issue_session_token(
                user_id, secret=TEST_JWT_SECRET, now=datetime.now(UTC) + timedelta(seconds=2)
            ),
            secret=TEST_JWT_SECRET,
        )
        assert after is not None
        assert identity.session_is_live(conn, user_id=user_id, issued_at=after.issued_at)
        # Unknown and erased accounts have no sessions to keep.
        assert identity.revoke_sessions(conn, uuid4()) is False
        assert not identity.session_is_live(conn, user_id=uuid4(), issued_at=datetime.now(UTC))

    def test_a_token_that_is_not_an_access_token_is_refused(self) -> None:
        # Correctly signed and dated, but typ names something else (or is
        # missing, as on any token minted before 0014): not a session.
        for payload in (
            {
                "sub": str(uuid4()),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(days=1),
            },
            {
                "sub": str(uuid4()),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(days=1),
                "typ": "refresh",
            },
        ):
            token = jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
            assert identity.session_user(token, secret=TEST_JWT_SECRET) is None
