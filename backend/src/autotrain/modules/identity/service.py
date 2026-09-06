"""The identity module's public API.

Identity owns users, auth and devices (ARCHITECTURE §3). Three faces today:

* Magic-link auth (§9): request_login mints a single-use token, stores only
  its hash, and mails the raw token through an injected EmailSender;
  verify_login spends the token and answers with a user id, creating the
  account on first login. issue_session_token/session_user mint and verify
  the JWTs that deps.current_user_id now checks on every request — these
  functions replaced the api layer's X-User-Id stub.
* user_profile: who a verified session belongs to, for the api's /auth/me;
  user_is_live: whether it still belongs to anyone, for the bearer gate.
* push_targets: where a user's push notifications can be delivered, for the
  notification worker.
* Forwarding addresses (0013): forwarding_code mints the personal part of
  the address a user forwards ticket emails to; user_by_forwarding_address
  is the reverse lookup the intake webhook does on every email.
* Session cutoff (0014): session_is_live is the bearer gate's per-request
  question, answering both "still an account" and "signed out everywhere
  since this token was issued"; revoke_sessions moves the cutoff.
* Erasure (GDPR): erase_user is the one composition point — it asks each
  module to forget the person, then strips and stamps the users row.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import jwt
import psycopg
from psycopg import errors as pg_errors

# Private alias for the same reason as in the other module services: the
# module object must not be reachable through this namespace, or callers could
# climb past every import-linter contract.
from autotrain.modules.identity import repository as _repository

# Re-exported: the shapes this service hands the worker and the api layer, so
# callers stay off identity.models (the identity-privacy contract).
from autotrain.modules.identity.models import PushTarget, SessionClaims, UserProfile
from autotrain.modules.journeys import service as _journeys

__all__ = [
    "SESSION_TTL",
    "EmailSender",
    "PushTarget",
    "SessionClaims",
    "UserProfile",
    "erase_user",
    "forwarding_address",
    "forwarding_code",
    "issue_session_token",
    "push_targets",
    "request_login",
    "revoke_sessions",
    "session_is_live",
    "session_user",
    "user_by_forwarding_address",
    "user_is_live",
    "user_profile",
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
# Public: the web session cookie's max-age is derived from it (routers/auth.py).
SESSION_TTL = timedelta(days=30)
# A verifier may run on a different host from the issuer; a few seconds of
# clock skew must not read as a token from the future.
_CLOCK_LEEWAY_SECONDS = 10


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    """Each user's registered devices, keyed by user id. Batched, like
    journeys.claim_contexts; a user with no devices is simply absent — the
    caller decides what an unreachable user means."""
    return _repository.push_targets(conn, user_ids)


def request_login(
    conn: psycopg.Connection, email: str, sender: EmailSender, *, app_base_url: str
) -> None:
    """Start a magic-link login: mint an unguessable single-use token, store
    only its hash (a leaked database can recognise tokens, never mint them),
    and email the raw token — the inbox holds the only copy in existence.

    Deliberately never checks whether the email has an account. Anyone may
    request a link for any address; only the inbox's owner can use it, and
    the uniform behaviour gives an attacker no way to probe which emails
    exist here (user enumeration).

    The link is <app_base_url>/login#token=<token>: /login is the frontend's
    route, a contract with the app rather than a detail of this module.
    """
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + _TOKEN_TTL
    _repository.insert_login_token(conn, email, token_hash, expires_at)
    sender.send_email(
        to=email,
        subject="Your AutoTrain login link",
        body=f"{app_base_url}/login#token={token}",
    )


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


def issue_session_token(user_id: UUID, *, secret: str, now: datetime | None = None) -> str:
    """Issue a session JWT: who (sub), when it was issued (iat, what the
    per-user cutoff is judged against), when it dies (exp), and what it is
    (typ, so a future refresh token can never pass as an access token).
    `now` is for tests that need a token from a chosen moment."""
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": issued,
        "exp": issued + SESSION_TTL,
        "typ": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def session_user(encoded_token: str, *, secret: str) -> SessionClaims | None:
    """The claims of a session token, or None if it is not one of ours: bad
    signature, expired, missing a claim, or not an access token. Only the
    signature and shape are judged here; whether the account and the
    cutoff still allow it is session_is_live's database question."""
    try:
        payload = jwt.decode(
            encoded_token,
            secret,
            algorithms=["HS256"],
            leeway=_CLOCK_LEEWAY_SECONDS,
            options={"require": ["sub", "exp", "iat"]},
        )
        if payload.get("typ") != "access":
            return None
        return SessionClaims(
            user_id=UUID(payload["sub"]),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        )
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return None


def user_profile(conn: psycopg.Connection, user_id: UUID) -> UserProfile | None:
    """The signed-in user's profile — None if the account is unknown or was
    erased after the session was issued."""
    return _repository.user_profile(conn, user_id)


def user_is_live(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Whether a verified session still names a usable account — False once
    the user is erased (GDPR) or was never created. A session outlives
    erasure by up to _SESSION_TTL, so the bearer gate asks this on every
    request: the ghost is refused everywhere, not only where a handler
    happens to load the user. One primary-key lookup per request."""
    return _repository.user_is_live(conn, user_id)


# tickets-<code>@<domain>. Codes are lowercase hex; the match is
# case-insensitive because mail systems may fold the local part either way.
_FORWARDING_ADDRESS = re.compile(r"^tickets-([a-z0-9]{6,64})@", re.IGNORECASE)
# 40 bits: unguessable by mail (the only way to use one), and the worst a
# guess achieves is a journey added to someone else's list.
_FORWARDING_CODE_BYTES = 5


def forwarding_code(conn: psycopg.Connection, user_id: UUID) -> str | None:
    """The user's forwarding code, minted on first call and stable after.
    None for an unknown or erased user.

    Minting is a guarded UPDATE: two first calls racing both end with the one
    code that won. A collision with another user's code (a unique index, one
    in a trillion) rolls back its savepoint and tries again.
    """
    for _ in range(3):
        row = _repository.forwarding_code(conn, user_id)
        if row is None:
            return None
        if row.forwarding_code is not None:
            return row.forwarding_code
        code = secrets.token_hex(_FORWARDING_CODE_BYTES)
        try:
            with conn.transaction():
                if _repository.claim_forwarding_code(conn, user_id, code):
                    return code
        except pg_errors.UniqueViolation:
            continue
    raise RuntimeError(f"could not mint a forwarding code for user {user_id}")


def forwarding_address(code: str, domain: str) -> str:
    """The address the user forwards ticket emails to."""
    return f"tickets-{code}@{domain}"


def user_by_forwarding_address(conn: psycopg.Connection, address: str) -> UUID | None:
    """The user a forwarding address belongs to — None for any address that
    is not one of ours, or whose code no live account holds."""
    match = _FORWARDING_ADDRESS.match(address.strip())
    if match is None:
        return None
    return _repository.user_id_by_forwarding_code(conn, match.group(1).lower())


def session_is_live(conn: psycopg.Connection, *, user_id: UUID, issued_at: datetime) -> bool:
    """The bearer gate's one database read: False once the account is erased
    (or never existed), and False for a session issued before the user's
    cutoff — the moment they last signed out everywhere. A token issued in
    the same second as the cutoff passes: iat is whole seconds, and the
    user signing straight back in must not be refused."""
    gate = _repository.session_gate(conn, user_id)
    if gate is None:
        return False
    cutoff = gate.sessions_invalid_before
    return cutoff is None or issued_at >= cutoff


def revoke_sessions(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Sign out everywhere: every session issued before now is refused from
    here on. False for an unknown or erased account."""
    return _repository.revoke_sessions(conn, user_id)


def erase_user(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Erase a person (GDPR), keeping what must be kept.

    0004's rule is anonymise, not delete: claims filed on the user's behalf
    stay for audit and dispute (their FKs are RESTRICT), so the users row
    survives with every identifying column nulled and deleted_at set. What
    goes, in order: everything the journeys module holds that came from the
    person (forwarded emails, seller and email references on tickets — see
    journeys.forget_user); pending login tokens for the email; devices; then
    the email, name, forwarding code, password and every session.

    False if there was no live account to erase — a second call is a no-op.
    tests/test_erasure.py scans every text column afterwards; a migration
    that adds a place personal data lives must extend one of the forget
    steps or that test fails.
    """
    email = _repository.user_email(conn, user_id)
    if email is None:
        return False
    _journeys.forget_user(conn, user_id)
    _repository.delete_login_tokens(conn, email)
    _repository.delete_devices(conn, user_id)
    return _repository.erase_user_row(conn, user_id)
