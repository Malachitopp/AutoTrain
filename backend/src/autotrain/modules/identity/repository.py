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
from autotrain.modules.identity.models import PushTarget, UserProfile

# Batched like journeys' _CLAIM_CONTEXTS: the worker resolves a whole page of
# detections' users in one round trip. Ordered so a user's devices come back
# in registration order — deterministic for tests, irrelevant to delivery.
_PUSH_TARGETS = (
    "SELECT user_id, platform, push_token FROM devices "
    "WHERE user_id = ANY(%s) ORDER BY user_id, created_at, id"
)

# The two noqas: bandit's S105 sees "TOKEN" in a name and suspects a hardcoded
# credential — these are SQL statements about tokens, not tokens.
_INSERT_LOGIN_TOKEN = "INSERT INTO login_tokens (email, token_hash, expires_at) VALUES (%s, %s, %s)"  # noqa: S105

_SPEND_LOGIN_TOKEN = (
    "UPDATE login_tokens "  # noqa: S105 — the directive sits on the diagnostic's line
    "SET used_at = now() "
    "WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() "
    "RETURNING email"
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


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    rows = db.fetch_all(conn, _PUSH_TARGETS, (list(user_ids),), row_cls=PushTarget)
    grouped: dict[UUID, list[PushTarget]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)
    return grouped


def insert_login_token(
    conn: psycopg.Connection, email: str, token_hash: str, expires_at: datetime
) -> None:
    """Store a pending magic-link login: the token hash, never the token."""

    db.execute(conn, _INSERT_LOGIN_TOKEN, (email, token_hash, expires_at))


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
