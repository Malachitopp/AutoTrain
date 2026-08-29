"""The identity module's public API.

Identity owns users, auth and devices (ARCHITECTURE §3). Auth — magic-link +
JWT (§9) — lands here and replaces the api layer's X-User-Id stub wholesale.
Until then the module exposes exactly one thing: where a user's push
notifications can be delivered, for the notification worker. Deliberately
minimal — building auth's surface before auth exists would be scaffolding to
maintain, not a seam to use.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import psycopg

# Private alias for the same reason as in the other module services: the
# module object must not be reachable through this namespace, or callers could
# climb past every import-linter contract.
from autotrain.modules.identity import repository as _repository

# Re-exported: the shape this service hands the notification worker, so
# callers stay off identity.models (the identity-privacy contract).
from autotrain.modules.identity.models import PushTarget

__all__ = [
    "PushTarget",
    "push_targets",
]


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    """Each user's registered devices, keyed by user id. Batched, like
    journeys.claim_contexts; a user with no devices is simply absent — the
    caller decides what an unreachable user means."""
    return _repository.push_targets(conn, user_ids)
