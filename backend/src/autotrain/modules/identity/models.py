"""Row shapes for the identity module's tables.

Identity owns users and devices (ARCHITECTURE §3). Only the slice the module
currently serves is modelled — more shapes arrive with auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PushTarget:
    """One deliverable device for one user — the notification worker's view
    of `devices` (0004). A token identifies one device install; platform
    rides along because delivery transports differ per platform (FCM/APNs)."""

    user_id: UUID
    platform: str
    push_token: str
