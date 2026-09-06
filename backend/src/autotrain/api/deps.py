"""Request-scoped dependencies.

The `Connection` annotation comes from `core.db`'s re-export: the api layer
passes connections through to module services but never touches psycopg
itself — no SQL, no driver errors, no driver imports (ARCHITECTURE §3).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, Header, HTTPException, Request

from autotrain.api.middleware import RequestTransaction
from autotrain.core.config import get_settings
from autotrain.core.db import Connection
from autotrain.modules.identity import service as identity

# The web app's session cookie. httpOnly (set in routers/auth.py), so page
# scripts never see the token and an XSS hole cannot lift it; the browser
# sends it by itself. The mobile app (later) uses the bearer header instead.
SESSION_COOKIE = "autotrain_session"


def get_conn(request: Request) -> Connection:
    """One request = one transaction, opened on first use.

    The transaction is owned by TransactionMiddleware (see middleware.py),
    which commits it BEFORE the response goes out and rolls it back on any
    error response — a yield dependency cannot do this, because FastAPI runs
    its teardown after the body is already on the wire, too late to turn a
    failed COMMIT into an error the client sees. A handler that creates a
    ticket and a journey still can never persist half.
    """
    txn: RequestTransaction = request.state.db_txn
    return txn.conn()


ConnDep = Annotated[Connection, Depends(get_conn)]


def current_user_id(
    conn: ConnDep,
    authorization: Annotated[str | None, Header()] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> UUID:
    """The authenticated user, from `Authorization: Bearer <jwt>` or from
    the session cookie — the same token either way; the header wins when
    both are present, so a tool or the mobile app can always say exactly
    which session it means.

    Every credential failure — nothing presented, wrong scheme, tampered or
    expired token, a session issued before the user's cutoff, an account
    since erased — collapses to the same 401 on purpose: distinguishing WHY
    a token failed only helps an attacker probing. The one exception is 503
    for a missing signing secret, which is an operations problem, not the
    caller's.

    The liveness check is the gate's one database read: it answers both
    "does the account still exist" and "has the user signed out everywhere
    since this token was issued" (identity.session_is_live).
    """
    if authorization is not None:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="invalid Authorization header")
        token = authorization.removeprefix("Bearer ")
    elif session_cookie is not None:
        token = session_cookie
    else:
        raise HTTPException(status_code=401, detail="missing session")

    secret = get_settings().jwt_secret
    if secret is None:
        raise HTTPException(status_code=503, detail="no JWT secret configured")

    claims = identity.session_user(token, secret=secret.get_secret_value())
    if claims is None or not identity.session_is_live(
        conn, user_id=claims.user_id, issued_at=claims.issued_at
    ):
        raise HTTPException(status_code=401, detail="invalid token")

    return claims.user_id


UserIdDep = Annotated[UUID, Depends(current_user_id)]
