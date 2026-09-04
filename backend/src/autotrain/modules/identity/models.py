"""Row shapes for the identity module's queries — deliberately not its tables.

Frozen dataclasses matched by column name (class_row), as in the other
modules. Identity owns users, auth and devices (ARCHITECTURE §3); each shape
here is one caller's view of a table, never the whole row, so columns that
must not leave the module (password_hash, deleted_at) cannot leak by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class PushTarget:
    """One deliverable device for one user — the notification worker's view
    of `devices` (0004). A token identifies one device install; platform
    rides along because delivery transports differ per platform (FCM/APNs)."""

    user_id: UUID
    platform: str
    push_token: str


@dataclass(frozen=True)
class UserProfile:
    """The /auth/me query's shape — not the users table. Deliberately omits
    password_hash and deleted_at; keeps claim_consent_at because the frontend
    decides from it whether to ask for auto-filing consent."""

    id: UUID
    email: str
    claim_consent_at: datetime | None
    created_at: datetime
