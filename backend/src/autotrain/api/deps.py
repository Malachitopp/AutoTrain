"""Request-scoped dependencies.

The `Connection` annotation comes from `core.db`'s re-export: the api layer
passes connections through to module services but never touches psycopg
itself — no SQL, no driver errors, no driver imports (ARCHITECTURE §3).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request

from autotrain.api.middleware import RequestTransaction
from autotrain.core.config import get_settings
from autotrain.core.db import Connection
from autotrain.modules.identity import service as identity


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


def current_user_id(conn: ConnDep, authorization: Annotated[str | None, Header()] = None) -> UUID:
    """The authenticated user, from the `Authorization: Bearer <jwt>` header.

    Replaced the X-User-Id development stub. Every credential failure —
    missing header, wrong scheme, tampered or expired token, a session whose
    account has since been erased — collapses to the same 401 on purpose:
    distinguishing WHY a token failed only helps an attacker probing. The
    one exception is 503 for a missing signing secret, which is an
    operations problem, not the caller's.

    The liveness check is the gate's one database read. Without it a
    session would outlive GDPR erasure on every route but /auth/me.
    """
    if authorization is None:
        raise HTTPException(status_code=401, detail="missing Bearer token")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid Authorization header")
    token = authorization.removeprefix("Bearer ")
    secret = get_settings().jwt_secret
    if secret is None:
        raise HTTPException(status_code=503, detail="no JWT secret configured")

    user_id = identity.session_user(token, secret=secret.get_secret_value())
    if user_id is None or not identity.user_is_live(conn, user_id=user_id):
        raise HTTPException(status_code=401, detail="invalid token")

    return user_id


UserIdDep = Annotated[UUID, Depends(current_user_id)]
