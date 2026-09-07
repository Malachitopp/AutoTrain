"""Three pure-ASGI middlewares: the request id (bottom of this file), the
per-caller rate limit, and the transaction-per-request, settled BEFORE the
response leaves the server.

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

import logging
import re
import time
from contextlib import AbstractContextManager
from uuid import uuid4

from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autotrain.core import db
from autotrain.core.db import Connection
from autotrain.core.observability import request_id_var

logger = logging.getLogger(__name__)

# A supplied X-Request-ID is kept only when it looks like an id: no newlines
# or control characters into the log, no 4KB headers echoed back.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# How many distinct callers the rate limiter remembers before it starts
# evicting the ones seen longest ago. A hard ceiling, not a hint: with each
# caller holding at most `limit` timestamps, the whole structure cannot pass
# roughly 8 MB no matter what arrives. Well above any traffic this product
# will see, and far below what would trouble the process.
_MAX_TRACKED_CLIENTS = 10_000


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


class RateLimitMiddleware:
    """Caps how often one caller may hit the endpoints that send email.

    Placement is the whole design, and it is why this is middleware rather
    than a dependency. Registered OUTSIDE TransactionMiddleware, it answers
    429 before a transaction has been opened — so the rollback-on-4xx rule
    never touches its bookkeeping. A limiter that recorded attempts inside
    the request transaction would have every one of them rolled back by the
    429 it just produced, forget every attempt it blocked, and let the next
    request through as though it were the first. Registered INSIDE
    CORSMiddleware, its 429 still gets CORS headers, so a browser can read
    the refusal and say "wait a minute" instead of showing a network error.

    The counter lives in this process's memory, not the database. The
    database would survive restarts and be shared between API processes, but
    middleware sits outside the request's transaction and would need a
    SECOND pooled connection for every request — halving what the pool can
    serve, to answer "yes" almost every time. Memory costs nothing and is
    honest about what it is: a cheap first filter that resets on deploy. The
    durable caps that must not be bypassed live in identity.request_login,
    counted from committed rows.

    What this catches that those cannot: one caller working through a
    thousand DIFFERENT addresses. A per-address cap never sees it, and it is
    the shape both a hostile script and a looping frontend actually take.

    The client address comes from the connection itself, never from an
    X-Forwarded-For header. A header is written by whoever is calling, so
    trusting one lets an attacker present a fresh address per request and
    walk straight through. That does mean this counts the proxy, not the
    user, once the API is deployed behind one — at which point the proxy's
    own rate limiting is the right tool and this becomes a backstop.
    """

    def __init__(
        self, app: ASGIApp, *, limit: int, window_seconds: float, paths: frozenset[str]
    ) -> None:
        self.app = app
        self._limit = limit
        self._window = window_seconds
        self._paths = paths
        self._hits: dict[str, list[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self._paths:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        if client is None:  # no peer address (a test transport, a unix socket)
            await self.app(scope, receive, send)
            return

        if self._over_limit(client[0]):
            logger.warning("rate limited", extra={"path": scope.get("path")})
            response = JSONResponse(
                {"detail": "too many login requests; try again shortly"},
                status_code=429,
                # An upper bound, not a promise: the window is rolling, so
                # the caller is usually free again sooner.
                headers={"Retry-After": str(int(self._window))},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _over_limit(self, host: str) -> bool:
        """Record this hit and say whether the caller is over their share.

        Every line of this runs on the event loop between awaits, so no two
        requests interleave inside it and no lock is needed. Monotonic time,
        so a clock correction cannot hand out a free window.

        Bounded in both directions, which a rate limiter has to be or it is
        an easier attack than the one it prevents:

        * A caller already over the cap is NOT recorded. Their list stops at
          `limit` entries instead of growing for as long as they keep
          knocking — and since everything they send is inside the window,
          nothing would ever expire out of it. The behavioural consequence
          is deliberate and standard for a sliding window: knocking while
          blocked does not extend the block, so capacity returns as the
          oldest hits age out.
        * The dictionary is capped by evicting whoever was seen longest ago.
          A dict keeps insertion order, so popping the caller and putting it
          back places it last, which leaves the FIRST key as the least
          recently seen. That makes eviction O(1) — no scan, no sort. The
          previous version rebuilt the whole dictionary looking for expired
          entries, which ran on every request once full, found nothing
          during the address-cycling attack it was meant to survive (every
          entry is live), and turned attack traffic into quadratic work
          here.

        The ceiling is therefore `limit` timestamps for each of
        _MAX_TRACKED_CLIENTS hosts: about 8 MB, whatever arrives.
        """
        now = time.monotonic()
        cutoff = now - self._window
        hits = [h for h in self._hits.pop(host, ()) if h > cutoff]
        over = len(hits) >= self._limit
        if not over:
            hits.append(now)
        self._hits[host] = hits
        while len(self._hits) > _MAX_TRACKED_CLIENTS:
            del self._hits[next(iter(self._hits))]
        return over


class RequestIdMiddleware:
    """Gives every request an id, returns it as X-Request-ID, and writes one
    line per request (method, path, status, duration) under that id — the
    line a user's "I got an error" is matched against. Registered outside
    TransactionMiddleware, so a request that fails before its transaction
    opens is still logged with its id. A client may supply its own id (a
    proxy, a retrying app); it is kept when it looks like one.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("x-request-id")
        request_id = supplied if supplied and _REQUEST_ID.fullmatch(supplied) else uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status: int | None = None

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            if scope.get("path") != "/healthz":  # the probe every few seconds is noise
                logger.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        # None here means an exception escaped before any
                        # response started; the outer error handler sends 500.
                        "status": status if status is not None else 500,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            request_id_var.reset(token)
