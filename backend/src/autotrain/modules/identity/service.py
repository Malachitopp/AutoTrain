"""The identity module's public API.

Identity owns users, auth and devices (ARCHITECTURE §3). Two faces today:

* Magic-link auth (§9): request_login mints a single-use token, stores only
  its hash, and mails the raw token through an injected EmailSender;
  verify_login spends the token and answers with a user id, creating the
  account on first login. issue_session_token/session_user mint and verify
  the JWTs that deps.current_user_id now checks on every request — these
  functions replaced the api layer's X-User-Id stub.
* push_targets: where a user's push notifications can be delivered, for the
  notification worker.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import jwt
import psycopg

# Private alias for the same reason as in the other module services: the
# module object must not be reachable through this namespace, or callers could
# climb past every import-linter contract.
from autotrain.modules.identity import repository as _repository

# Re-exported: the shape this service hands the notification worker, so
# callers stay off identity.models (the identity-privacy contract).
from autotrain.modules.identity.models import PushTarget

__all__ = [
    "EmailSender",
    "PushTarget",
    "issue_session_token",
    "push_targets",
    "request_login",
    "session_user",
    "verify_login",
]


class EmailSender(Protocol):
    """Anything that can deliver one email.

    Implemented outside the module (sources/email.py) and injected by the
    api layer — the same seam as notifications' PushSender. Implementations
    may raise: request_login runs inside the caller's transaction, so a
    failed send rolls the token insert back with it and no orphaned token
    outlives its email. They must bound their own delivery time (network
    timeouts) — a request handler is waiting on send_email.
    """

    def send_email(self, *, to: str, subject: str, body: str) -> None: ...


_TOKEN_TTL = timedelta(minutes=15)
_SESSION_TTL = timedelta(days=30)


class IdentityError(Exception):
    """Base for identity domain failures."""


class EmailSendFailure(IdentityError):
    """The email sender failed to deliver the login link."""


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    """Each user's registered devices, keyed by user id. Batched, like
    journeys.claim_contexts; a user with no devices is simply absent — the
    caller decides what an unreachable user means."""
    return _repository.push_targets(conn, user_ids)


def request_login(conn: psycopg.Connection, email: str, sender: EmailSender) -> None:
    """Start a magic-link login: mint an unguessable single-use token, store
    only its hash (a leaked database can recognise tokens, never mint them),
    and email the raw token — the inbox holds the only copy in existence.

    Deliberately never checks whether the email has an account. Anyone may
    request a link for any address; only the inbox's owner can use it, and
    the uniform behaviour gives an attacker no way to probe which emails
    exist here (user enumeration).
    """
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + _TOKEN_TTL
    _repository.insert_login_token(conn, email, token_hash, expires_at)
    sender.send_email(
        to=email,
        subject="Your AutoTrain login link",
        body=f"Click here to log in: https://autotrain.example.com/login?token={token}",
    )  # TODO: real link once frontend exists


def verify_login(conn: psycopg.Connection, token: str) -> UUID | None:
    """Exchange a clicked magic-link token for a user id — None if the token
    is unknown, expired, or already spent. The guarded UPDATE judges all
    three at once and race-safely: two concurrent clicks on one link log in
    exactly one caller. First login creates the account — signup and login
    are deliberately the same act, and clicking the link is what proves the
    address is real.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    email = _repository.spend_login_token(conn, token_hash)
    if email is None:
        return None
    user_id = _repository.user_id_by_email(conn, email)
    if user_id is None:
        user_id = _repository.create_user(conn, email)
    return user_id


def issue_session_token(user_id: UUID, *, secret: str) -> str:
    """Issue a JWT that encodes the user id and an expiration"""
    payload = {"sub": str(user_id), "exp": datetime.now(UTC) + _SESSION_TTL}
    return jwt.encode(payload, secret, algorithm="HS256")


def session_user(encoded_token: str, *, secret: str) -> UUID | None:
    """decodes a JWT and returns the userid if its valid otherwise its None"""
    try:
        payload = jwt.decode(encoded_token, secret, algorithms=["HS256"])
        return UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None
