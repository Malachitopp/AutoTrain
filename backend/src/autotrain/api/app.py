"""FastAPI application factory.

A factory rather than a module-level app so tests can build as many isolated
instances as they need (each with its own dependency overrides), and so uvicorn
constructs the app after the process has its environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from autotrain.api.middleware import TransactionMiddleware
from autotrain.api.routers import claims, journeys, auth
from autotrain.core import db


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Open the pool at boot so an unreachable database fails the task loudly
    # at startup, not on the first request minutes later.
    db.init_pool()
    yield
    db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="AutoTrain API", version="0.1.0", lifespan=_lifespan)
    # Owns the per-request transaction so the COMMIT happens before the
    # response's first bytes leave the server (see middleware.py for why a
    # yield dependency cannot provide that ordering).
    app.add_middleware(TransactionMiddleware)
    app.include_router(journeys.router)
    app.include_router(claims.router)
    app.include_router(auth.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Liveness only, no database touch: a saturated pool must not make
        # the orchestrator kill otherwise-healthy tasks.
        return {"status": "ok"}

    return app
