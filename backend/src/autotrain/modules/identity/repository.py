"""SQL for the identity module — same rules as every other repository.

Constants are deliberately unannotated so pyright keeps their LiteralString
type; values travel as `%s` parameters, never in the text. Functions take the
connection first and never commit — the caller owns the transaction.

Scope note: only identity-owned tables appear here (users, devices — 0004;
login_tokens — 0012).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import psycopg

from autotrain.core import db
from autotrain.modules.identity.models import (
    ForwardingCode,
    ForwardingOwner,
    PushTarget,
    SessionGate,
    UserProfile,
)

# Batched like journeys' _CLAIM_CONTEXTS: the worker resolves a whole page of
# detections' users in one round trip. Ordered so a user's devices come back
# in registration order — deterministic for tests, irrelevant to delivery.
_PUSH_TARGETS = (
    "SELECT user_id, platform, push_token FROM devices "
    "WHERE user_id = ANY(%s) ORDER BY user_id, created_at, id"
)

# The two noqas: bandit's S105 sees "TOKEN" in a name and suspects a hardcoded
# credential — these are SQL statements about tokens, not tokens.
_INSERT_LOGIN_TOKEN = (
    "INSERT INTO login_tokens (email, email_key, token_hash, expires_at) "  # noqa: S105
    "VALUES (%s, %s, %s, %s)"
)

_SPEND_LOGIN_TOKEN = (
    "UPDATE login_tokens "  # noqa: S105 — the directive sits on the diagnostic's line
    "SET used_at = now() "
    "WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() "
    "RETURNING email"
)
# The login caps (0018, 0019). Both count rows that are already here: a
# request that sends an email commits its rows, so the committed rows are
# the record of what went out, and a refusal writes nothing — which is what
# makes this survive TransactionMiddleware rolling back every response of
# 400 and above.
#
# Two tables, because the two questions have different owners. "How many
# links has this INBOX been sent" keys on email_key, the address reduced to
# where it lands, so a plus-suffix cannot buy a fresh budget. "How many have
# we sent today" counts login_sends, which holds a timestamp and nothing
# else: erasure deletes a person's login_tokens and must, and a daily cap
# counted from that table fell by five every time an account was deleted.
#
# The inner LIMIT is the same trick as journeys' intake cap: the answer is
# only ever compared against a small cap, so counting past it is work whose
# result is thrown away. Under attack that is the difference between a count
# over the day's whole traffic and one that stops at five.
_COUNT_LOGIN_TOKENS_FOR_INBOX = (
    "SELECT count(*) FROM (SELECT 1 FROM login_tokens "
    "WHERE email_key = %s AND created_at >= %s LIMIT %s) AS capped"
)
_COUNT_LOGIN_SENDS_SINCE = (
    "SELECT count(*) FROM (SELECT 1 FROM login_sends WHERE created_at >= %s LIMIT %s) AS capped"
)
_INSERT_LOGIN_SEND = "INSERT INTO login_sends DEFAULT VALUES"
# Transaction-scoped, unlike job_lock's session-level lock: it is released
# by the commit or rollback the middleware performs either way, so it can
# never be leaked by a failing request. Counting and then inserting is a
# race without it — every concurrent request counts under the cap, and all
# of them insert.
_LOCK_LOGIN_REQUESTS = "SELECT pg_advisory_xact_lock(%s)"
# Batched like journeys' retention purge, so a first run over a long backlog
# never holds one long transaction.
_DELETE_OLD_LOGIN_TOKENS = (
    "DELETE FROM login_tokens WHERE id IN ("
    "SELECT id FROM login_tokens WHERE created_at < %s ORDER BY created_at LIMIT %s)"
)
_DELETE_OLD_LOGIN_SENDS = (
    "DELETE FROM login_sends WHERE id IN ("
    "SELECT id FROM login_sends WHERE created_at < %s ORDER BY created_at LIMIT %s)"
)

_USER_ID_EMAIL = "SELECT id FROM users WHERE email = %s AND deleted_at IS NULL"
_CREATE_USER = "INSERT INTO users (email) VALUES (%s) RETURNING id"

# deleted_at IS NULL: a session can outlive GDPR erasure, and an erased
# account must read as gone (None) — never resurrect through a stale token.
_USER_PROFILE = (
    "SELECT id, email, claim_consent_at, created_at FROM users WHERE id = %s AND deleted_at IS NULL"
)
# The bearer gate's per-request question. EXISTS rather than the profile
# row: the gate needs one bit, and it runs on every authenticated request.
_USER_IS_LIVE = "SELECT EXISTS (SELECT 1 FROM users WHERE id = %s AND deleted_at IS NULL)"

# Forwarding codes (0013). The claim is guarded on IS NULL so two requests
# minting at once both keep the first code; the loser reads it back.
# Rotation is not guarded: a new code replaces whatever was there, and mail
# to the old address is nobody's from then on.
_FORWARDING_CODE = "SELECT forwarding_code FROM users WHERE id = %s AND deleted_at IS NULL"
_CLAIM_FORWARDING_CODE = (
    "UPDATE users SET forwarding_code = %s "
    "WHERE id = %s AND forwarding_code IS NULL AND deleted_at IS NULL"
)
_ROTATE_FORWARDING_CODE = (
    "UPDATE users SET forwarding_code = %s WHERE id = %s AND deleted_at IS NULL"
)
_USER_BY_FORWARDING_CODE = (
    "SELECT id, email FROM users WHERE forwarding_code = %s AND deleted_at IS NULL"
)

# The session gate (0014): one read answers "still an account" (a row) and
# "signed out everywhere since this token" (the cutoff). Cutoffs are set
# from the wall clock, not the transaction clock, and truncated to the
# second because a token's iat is whole seconds.
_SESSION_GATE = "SELECT sessions_invalid_before FROM users WHERE id = %s AND deleted_at IS NULL"
_REVOKE_SESSIONS = (
    "UPDATE users SET sessions_invalid_before = date_trunc('second', clock_timestamp()) "
    "WHERE id = %s AND deleted_at IS NULL"
)

# Erasure (0004's anonymise-not-delete). The email is read first so the
# pending login tokens for it can go; then the row is stripped and stamped.
_USER_EMAIL = "SELECT email FROM users WHERE id = %s AND deleted_at IS NULL"
# By inbox, not by the address as typed: the person forgotten as a@x is the
# person the links to a+shop@x were sent to, and those rows carry their
# address too.
_DELETE_LOGIN_TOKENS = "DELETE FROM login_tokens WHERE email_key = %s"
_DELETE_DEVICES = "DELETE FROM devices WHERE user_id = %s"
_ERASE_USER = (
    "UPDATE users SET email = NULL, display_name = NULL, password_hash = NULL, "
    "forwarding_code = NULL, deleted_at = now(), "
    "sessions_invalid_before = date_trunc('second', clock_timestamp()) "
    "WHERE id = %s AND deleted_at IS NULL"
)


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    rows = db.fetch_all(conn, _PUSH_TARGETS, (list(user_ids),), row_cls=PushTarget)
    grouped: dict[UUID, list[PushTarget]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)
    return grouped


def insert_login_token(
    conn: psycopg.Connection, email: str, email_key: str, token_hash: str, expires_at: datetime
) -> None:
    """Store a pending magic-link login: the token hash, never the token."""

    db.execute(conn, _INSERT_LOGIN_TOKEN, (email, email_key, token_hash, expires_at))


def spend_login_token(conn: psycopg.Connection, token_hash: str) -> str | None:
    """Stamp the token used and return its email — None if unknown, expired,
    or already spent. Race-safe: two concurrent clicks yield exactly one email."""
    return db.fetch_value(conn, _SPEND_LOGIN_TOKEN, (token_hash,))


def user_id_by_email(conn: psycopg.Connection, email: str) -> UUID | None:
    """The living account for an email, if any; erased accounts never match."""
    return db.fetch_value(conn, _USER_ID_EMAIL, (email,))


def create_user(conn: psycopg.Connection, email: str) -> UUID:
    """Create a user knowing only their email. Consent columns start NULL:
    signup is not consent — that is granted explicitly in the app and stamped
    when it happens."""
    return db.fetch_value(conn, _CREATE_USER, (email,))


def user_profile(conn: psycopg.Connection, user_id: UUID) -> UserProfile | None:
    """The /auth/me row for a user — None if unknown or erased."""
    return db.fetch_one(conn, _USER_PROFILE, (user_id,), row_cls=UserProfile)


def user_is_live(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Whether user_id names an account that may still act — False if it
    was never created or has been erased. Always a bool: EXISTS yields a
    row either way, so this never has a None arm to handle."""
    return db.fetch_value(conn, _USER_IS_LIVE, (user_id,))


def forwarding_code(conn: psycopg.Connection, user_id: UUID) -> ForwardingCode | None:
    """None for an unknown or erased user; a row (possibly holding None) otherwise."""
    return db.fetch_one(conn, _FORWARDING_CODE, (user_id,), row_cls=ForwardingCode)


def claim_forwarding_code(conn: psycopg.Connection, user_id: UUID, code: str) -> bool:
    """True if this call set the code; False if the user already had one."""
    return db.execute(conn, _CLAIM_FORWARDING_CODE, (code, user_id)) == 1


def rotate_forwarding_code(conn: psycopg.Connection, user_id: UUID, code: str) -> bool:
    """True if a live account's code was replaced; False if there was none."""
    return db.execute(conn, _ROTATE_FORWARDING_CODE, (code, user_id)) == 1


def user_by_forwarding_code(conn: psycopg.Connection, code: str) -> ForwardingOwner | None:
    return db.fetch_one(conn, _USER_BY_FORWARDING_CODE, (code,), row_cls=ForwardingOwner)


def session_gate(conn: psycopg.Connection, user_id: UUID) -> SessionGate | None:
    """None for an unknown or erased account; otherwise the cutoff row."""
    return db.fetch_one(conn, _SESSION_GATE, (user_id,), row_cls=SessionGate)


def revoke_sessions(conn: psycopg.Connection, user_id: UUID) -> bool:
    """True if a live account's cutoff was moved to now."""
    return db.execute(conn, _REVOKE_SESSIONS, (user_id,)) == 1


def user_email(conn: psycopg.Connection, user_id: UUID) -> str | None:
    return db.fetch_value(conn, _USER_EMAIL, (user_id,))


def lock_login_requests(conn: psycopg.Connection) -> None:
    """Serialise login requests for the rest of this transaction."""
    db.fetch_value(conn, _LOCK_LOGIN_REQUESTS, (db.lock_key("login-request"),))


def count_login_tokens_for_inbox(
    conn: psycopg.Connection, email_key: str, since: datetime, cap: int
) -> int:
    """How many links this inbox has been sent since `since`, counted no
    further than `cap`."""
    return db.fetch_value(conn, _COUNT_LOGIN_TOKENS_FOR_INBOX, (email_key, since, cap))


def count_login_sends_since(conn: psycopg.Connection, since: datetime, cap: int) -> int:
    """How many login emails have gone out since `since`, counted no further
    than `cap`."""
    return db.fetch_value(conn, _COUNT_LOGIN_SENDS_SINCE, (since, cap))


def record_login_send(conn: psycopg.Connection) -> None:
    db.execute(conn, _INSERT_LOGIN_SEND)


def delete_old_login_tokens(conn: psycopg.Connection, before: datetime, limit: int) -> int:
    """How many tokens this call deleted."""
    return db.execute(conn, _DELETE_OLD_LOGIN_TOKENS, (before, limit))


def delete_old_login_sends(conn: psycopg.Connection, before: datetime, limit: int) -> int:
    """How many send records this call deleted."""
    return db.execute(conn, _DELETE_OLD_LOGIN_SENDS, (before, limit))


def delete_login_tokens(conn: psycopg.Connection, email_key: str) -> int:
    return db.execute(conn, _DELETE_LOGIN_TOKENS, (email_key,))


def delete_devices(conn: psycopg.Connection, user_id: UUID) -> int:
    return db.execute(conn, _DELETE_DEVICES, (user_id,))


def erase_user_row(conn: psycopg.Connection, user_id: UUID) -> bool:
    """True if this call erased a live account; False if there was none."""
    return db.execute(conn, _ERASE_USER, (user_id,)) == 1
