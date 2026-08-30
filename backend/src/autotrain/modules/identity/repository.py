"""SQL for the identity module — same rules as every other repository.

Constants are deliberately unannotated so pyright keeps their LiteralString
type; values travel as `%s` parameters, never in the text. Functions take the
connection first and never commit — the caller owns the transaction.

Scope note: only identity-owned tables appear here (users, devices — 0004).
Today that is one read; auth's queries land beside it.
"""

from __future__ import annotations

from collections.abc import Sequence
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


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    rows = db.fetch_all(conn, _PUSH_TARGETS, (list(user_ids),), row_cls=PushTarget)
    grouped: dict[UUID, list[PushTarget]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)
    return grouped
