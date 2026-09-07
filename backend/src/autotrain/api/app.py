"""FastAPI application factory.

A factory rather than a module-level app so tests can build as many isolated
instances as they need (each with its own dependency overrides), and so uvicorn
constructs the app after the process has its environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from autotrain.api.middleware import RequestIdMiddleware, TransactionMiddleware
from autotrain.api.routers import auth, claims, intake, journeys, operators
from autotrain.core import db
from autotrain.core.config import get_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Open the pool at boot so an unreachable database fails the task loudly
    # at startup, not on the first request minutes later.
    db.init_pool()
    yield
    db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="AutoTrain API", version="0.1.0", lifespan=_lifespan)
    settings = get_settings()
    # Owns the per-request transaction so the COMMIT happens before the
    # response's first bytes leave the server (see middleware.py for why a
    # yield dependency cannot provide that ordering).
    app.add_middleware(TransactionMiddleware)
    # Deliberately no per-caller rate limiter here. One was written and
    # removed: the application cannot reliably tell who is calling — it sees
    # whichever socket or header the deployment hands it, so the same code
    # keyed one attacker into 2^64 IPv6 buckets on one machine and every
    # user into a single bucket behind a proxy. Per-caller limiting belongs
    # at the edge, where the real client address is known, and is a
    # deployment requirement (see .env.example). What the application CAN
    # bound is its own spend, and identity.request_login does, from
    # committed rows.
    # Outside the transaction: browser preflight OPTIONS requests are
    # answered here and never open one. Origins come from config and default
    # to none. allow_credentials lets the browser send the session cookie
    # from those origins (and only those — a wildcard is refused with
    # credentials, by the standard); X-Request-ID is exposed so a page can
    # quote it when reporting a problem.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    # Outermost (the last middleware added wraps everything): every request,
    # preflights and failures included, gets an id and one log line.
    app.add_middleware(RequestIdMiddleware)
    app.include_router(journeys.router)
    app.include_router(claims.router)
    app.include_router(auth.router)
    app.include_router(operators.router)
    app.include_router(intake.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Liveness only, no database touch: a saturated pool must not make
        # the orchestrator kill otherwise-healthy tasks.
        return {"status": "ok"}

    return app
