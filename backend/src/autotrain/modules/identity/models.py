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


@dataclass(frozen=True)
class ForwardingCode:
    """One user's forwarding code, or None when none has been minted yet —
    a row shape so "no code" and "no such user" stay distinguishable."""

    forwarding_code: str | None


@dataclass(frozen=True)
class ForwardingOwner:
    """The live account a forwarding address belongs to — the webhook's
    lookup. The email rides along because the intake door
    (journeys.intake.screen) compares the sender against it."""

    id: UUID
    email: str


@dataclass(frozen=True)
class SessionGate:
    """The bearer gate's per-request read (0014): a row means the account is
    live; the cutoff, when set, is the instant before which no session is
    accepted."""

    sessions_invalid_before: datetime | None


@dataclass(frozen=True)
class SessionClaims:
    """What a verified session token says: who, and when it was issued —
    the two facts the gate judges it on."""

    user_id: UUID
    issued_at: datetime
