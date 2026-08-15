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
from autotrain.core.db import Connection


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


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> UUID:
    """DEVELOPMENT STUB — this is NOT authentication.

    It trusts an `X-User-Id` header outright so the API is exercisable before
    the identity module lands (magic-link + JWT, ARCHITECTURE §9). The identity
    PR replaces this dependency wholesale; nothing else may read the header.
    Note the stub can name a user that does not exist — services surface that
    as UnknownUser and routers answer 404, never a 500.
    """
    if x_user_id is None:
        raise HTTPException(status_code=401, detail="missing X-User-Id header")
    try:
        return UUID(x_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="malformed X-User-Id header") from exc


ConnDep = Annotated[Connection, Depends(get_conn)]
UserIdDep = Annotated[UUID, Depends(current_user_id)]
