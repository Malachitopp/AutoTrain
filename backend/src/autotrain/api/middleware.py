"""Transaction-per-request, settled BEFORE the response leaves the server.

Since FastAPI 0.106 the teardown of a yield dependency runs after the response
body is already on the wire. A dependency-owned `with db.transaction()`
therefore commits too late: the client can be holding a 201 for a journey
whose COMMIT then fails — a confirmed-but-lost write, visible only in server
logs, in a product whose bar is "the user checks this number against their
bank statement". This middleware owns the transaction instead and settles it
while a failure can still become a 500 the client actually sees.

The rules it implements:

* Lazy — a handler that never asks for a connection (healthz) never touches
  the pool, so a saturated pool cannot fail the liveness probe.
* Commit for < 400 responses, rollback for everything else — including
  HTTPExceptions that the exception middleware has already turned into 4xx
  responses by the time messages pass through here.
* The commit happens before `http.response.start` is forwarded: once the
  status line is out, it is too late to change the answer. A commit failure
  raises here, the response never starts, and the outer error middleware
  sends a 500.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

from fastapi.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autotrain.core import db
from autotrain.core.db import Connection


class _Rollback(Exception):
    """Sentinel thrown into db.transaction() to drive its rollback path."""


class RequestTransaction:
    """One request's lazily opened unit of work.

    `deps.get_conn` reads it off `request.state`; only TransactionMiddleware
    settles it. Handlers and services never commit — unchanged house rule,
    just owned one layer further out than a dependency can manage.
    """

    def __init__(self) -> None:
        self._cm: AbstractContextManager[Connection] | None = None
        self._conn: Connection | None = None

    @property
    def open(self) -> bool:
        return self._cm is not None

    def conn(self) -> Connection:
        if self._conn is None:
            self._cm = db.transaction()
            self._conn = self._cm.__enter__()
        return self._conn

    def settle(self, *, commit: bool) -> None:
        """Commit or roll back and return the connection to the pool.

        Idempotent, and a no-op when nothing was opened — so the error path
        can always call it again without double-releasing.
        """
        cm, self._cm, self._conn = self._cm, None, None
        if cm is None:
            return
        if commit:
            cm.__exit__(None, None, None)
        else:
            # Throwing the sentinel makes db.transaction() take the same
            # rollback path a handler exception would. Called directly (not
            # via `with`), __exit__ reports the exception by returning False
            # rather than re-raising, so nothing escapes here.
            cm.__exit__(_Rollback, _Rollback(), None)


class TransactionMiddleware:
    """Pure ASGI on purpose: only at the message level can code run between
    the handler finishing and the response starting."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        txn = RequestTransaction()
        scope.setdefault("state", {})["db_txn"] = txn

        async def send_settled(message: Message) -> None:
            if message["type"] == "http.response.start" and txn.open:
                # In a thread: psycopg is synchronous and COMMIT is network
                # I/O that must not block the event loop.
                await run_in_threadpool(txn.settle, commit=message["status"] < 400)
            await send(message)

        try:
            await self.app(scope, receive, send_settled)
        finally:
            # Still open here only when an exception escaped before any
            # response started; that path must never commit.
            if txn.open:
                await run_in_threadpool(txn.settle, commit=False)
