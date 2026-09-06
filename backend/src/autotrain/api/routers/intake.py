"""Ticket-email intake routes (ARCHITECTURE §6a).

Three routes, two audiences. The mail provider's webhook posts each email
that arrives at the inbound domain to POST /intake/email, authenticated by a
shared secret rather than a user session — there is no user on that end of
the line. The signed-in user reads their own forwarding address and what
became of the emails they sent to it.

Thin by rule (ARCHITECTURE §3), and a composition point like
routers/journeys.py: identity knows which user an address belongs to,
journeys owns the emails and what is read out of them. The webhook only
STORES — reading is the scheduler's intake sweep — so the provider gets its
202 in milliseconds and a slow reader can never make it retry.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query

from autotrain.api.deps import ConnDep, UserIdDep
from autotrain.api.schemas import (
    ForwardingAddressOut,
    InboundEmailIn,
    InboundEmailOut,
    IntakeReceipt,
)
from autotrain.core.config import get_settings
from autotrain.modules.identity import service as identity
from autotrain.modules.journeys import service as journeys

router = APIRouter(prefix="/intake", tags=["intake"])


def _require_intake_secret(presented: str | None) -> None:
    """The webhook's credential: one shared secret, compared in constant
    time. Missing configuration is a 503 (ours to fix); a wrong or absent
    header is a 401 with no further detail."""
    settings = get_settings()
    if settings.intake_secret is None:
        raise HTTPException(status_code=503, detail="no intake secret configured")
    expected = settings.intake_secret.get_secret_value()
    # Bytes, not str: compare_digest refuses non-ASCII text, and a header is
    # attacker-supplied — a stray character must be a 401, not a 500.
    if presented is None or not secrets.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid intake secret")


@router.post("/email", status_code=202)
def receive_email(
    payload: InboundEmailIn,
    conn: ConnDep,
    x_autotrain_intake_secret: Annotated[str | None, Header()] = None,
) -> IntakeReceipt:
    """Store one forwarded email for the intake sweep. Always 202 once the
    secret checks out: a provider retries on 4xx/5xx, and neither an unknown
    recipient nor a repeat delivery is something a retry would fix. The
    receipt says which of the three it was."""
    _require_intake_secret(x_autotrain_intake_secret)
    user_id = identity.user_by_forwarding_address(conn, payload.recipient)
    stored = journeys.receive_ticket_email(
        conn,
        user_id=user_id,
        message_id=payload.message_id,
        sender=payload.sender,
        recipient=payload.recipient,
        subject=payload.subject,
        body=payload.body,
    )
    if stored is None:
        return IntakeReceipt(status="duplicate")
    return IntakeReceipt(status="rejected" if user_id is None else "received")


@router.get("/address")
def forwarding_address(conn: ConnDep, user_id: UserIdDep) -> ForwardingAddressOut:
    """The address this user forwards ticket emails to, minted on first ask."""
    domain = get_settings().inbound_email_domain
    if not domain:
        # Blank counts as unset, as with app_base_url: an address with no
        # domain would be shown to the user and bounce.
        raise HTTPException(status_code=503, detail="no inbound email domain configured")
    code = identity.forwarding_code(conn, user_id)
    if code is None:
        raise HTTPException(status_code=404, detail="unknown user")
    return ForwardingAddressOut(address=identity.forwarding_address(code, domain))


@router.get("/emails")
def list_emails(
    conn: ConnDep,
    user_id: UserIdDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InboundEmailOut]:
    """What became of the emails this user forwarded, newest first."""
    rows = journeys.list_inbound_emails(conn, user_id, limit=limit)
    return [InboundEmailOut.model_validate(row) for row in rows]
