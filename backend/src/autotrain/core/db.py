"""Connection pooling and the query helpers every repository uses.

This is the whole data-access layer. There is no ORM and no query builder: modules
write SQL, hand it to these helpers with parameters, and get back plain dataclasses
(ARCHITECTURE §3, "Data access: hand-written SQL, no ORM").

Two rules the rest of the codebase depends on:

1. Every value goes in as a `%s` parameter. Never interpolate. The `sql` parameter
   is typed `LiteralString` (PEP 675), so a query built from a runtime value —
   f-string, concatenation, or read from a file — is a *type error*, not a code
   review note. `ruff`'s S608 is a second backstop for callers that skip these
   helpers. Note that annotating a query constant `: str` discards the guarantee;
   leave repository constants unannotated.
2. `fetch_one`/`fetch_all` map rows onto a dataclass by *column name*, so a
   `SELECT *` that picks up a new column will raise instead of silently returning
   the wrong shape. Name your columns explicitly in queries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, LiteralString, TypeVar

import psycopg
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from autotrain.core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One set of parameters. `None` means "this statement takes none".
Params = Sequence[Any] | Mapping[str, Any] | None
# `executemany` runs the statement once per element, so each element must be a
# real parameter set — `None` is not a valid member here.
ParamSet = Sequence[Any] | Mapping[str, Any]

_pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    """Open the process-wide pool. Call once from an entrypoint's startup hook."""
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    _pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_timeout_seconds,
        # Fail fast at boot if the database is unreachable, rather than on the
        # first request minutes later.
        open=True,
        name="autotrain",
    )
    _pool.wait(timeout=settings.db_pool_timeout_seconds)
    logger.info(
        "db pool open (min=%s max=%s)", settings.db_pool_min_size, settings.db_pool_max_size
    )
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("db pool not initialised — call init_pool() at process startup")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """Borrow a connection for one unit of work.

    Commits on clean exit, rolls back on any exception. Repositories never call
    `commit()` themselves — the caller decides what a transaction is, which is how
    two repository calls can be made atomic without either of them knowing.
    """
    with get_pool().connection() as conn:
        yield conn


def fetch_all(
    conn: psycopg.Connection,
    sql: LiteralString,
    params: Params = None,
    *,
    row_cls: type[T],
) -> list[T]:
    with conn.cursor(row_factory=class_row(row_cls)) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(
    conn: psycopg.Connection,
    sql: LiteralString,
    params: Params = None,
    *,
    row_cls: type[T],
) -> T | None:
    with conn.cursor(row_factory=class_row(row_cls)) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_value(conn: psycopg.Connection, sql: LiteralString, params: Params = None) -> Any:
    """Single scalar — counts, existence checks, `RETURNING id`."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return None if row is None else row[0]


def execute(conn: psycopg.Connection, sql: LiteralString, params: Params = None) -> int:
    """Run a statement and return the number of rows affected.

    The row count is load-bearing for idempotent writes: an
    `INSERT ... ON CONFLICT DO NOTHING` that returns 0 means we have already
    processed this event and the caller should stop, not retry.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_many(
    conn: psycopg.Connection, sql: LiteralString, params_seq: Sequence[ParamSet]
) -> None:
    with conn.cursor() as cur:
        cur.executemany(sql, params_seq)
