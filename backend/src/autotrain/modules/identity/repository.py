"""SQL for the identity module — same rules as every other repository.

Constants are deliberately unannotated so pyright keeps their LiteralString
type; values travel as `%s` parameters, never in the text. Functions take the
connection first and never commit — the caller owns the transaction.

Scope note: only identity-owned tables appear here (users, devices — 0004, login_tokens -- 0012).
Today that is one read; auth's queries land beside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import psycopg

from autotrain.core import db
from autotrain.modules.identity.models import PushTarget

# Batched like journeys' _CLAIM_CONTEXTS: the worker resolves a whole page of
# detections' users in one round trip. Ordered so a user's devices come back
# in registration order — deterministic for tests, irrelevant to delivery.
_PUSH_TARGETS = (
    "SELECT user_id, platform, push_token FROM devices "
    "WHERE user_id = ANY(%s) ORDER BY user_id, created_at, id"
)

_INSERT_LOGIN_TOKEN = "INSERT INTO login_tokens (email, token_hash, expires_at) VALUES (%s, %s, %s)"

_SPEND_LOGIN_TOKEN = (
    "UPDATE login_tokens "
    "SET used_at = now() "
    "WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() "
    "RETURNING email"
)
_USER_ID_EMAIL = "SELECT id FROM users WHERE email = %s AND deleted_at IS NULL"
_CREATE_USER = "INSERT INTO users (email) VALUES (%s) RETURNING id"


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

    db.execute(conn, _INSERT_LOGIN_TOKEN, (email, token_hash, expires_at))


def spend_login_token(conn: psycopg.Connection, token_hash: str) -> str | None:
    return db.fetch_value(conn, _SPEND_LOGIN_TOKEN, (token_hash,))


def user_id_by_email(conn: psycopg.Connection, email: str) -> UUID | None:
    return db.fetch_value(conn, _USER_ID_EMAIL, (email,))


def create_user(conn: psycopg.Connection, email: str) -> UUID:
    """consent forms for legality. Starts empty since users have to fill it in
    and it cant exist without a users input"""
    return db.fetch_value(conn, _CREATE_USER, (email,))
