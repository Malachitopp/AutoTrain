"""Test fixtures.

Every test runs against a real Postgres. We never mock the database — a mocked
query proves the Python around it is wired up and proves nothing about whether the
SQL is valid, which is the only part that can realistically be wrong
(ARCHITECTURE §9).

Start the database with `docker compose up -d postgres` from the repo root.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import conninfo, sql

DEFAULT_TEST_URL = "postgresql://autotrain:autotrain@localhost:5433/autotrain_test"

# The suite drops and recreates its database, so it must run against the test URL
# and nothing else. Set this before importing anything that reads settings.
TEST_DATABASE_URL = os.environ.get("AUTOTRAIN_TEST_DATABASE_URL", DEFAULT_TEST_URL)
os.environ["AUTOTRAIN_DATABASE_URL"] = TEST_DATABASE_URL

from autotrain.core import db  # noqa: E402
from autotrain.core.config import get_settings  # noqa: E402
from autotrain.core.migrate import migrate_up  # noqa: E402

get_settings.cache_clear()


def _maintenance_conninfo() -> str:
    """Connection string for the `postgres` database — you cannot drop the
    database you are currently connected to."""
    params = conninfo.conninfo_to_dict(TEST_DATABASE_URL)
    params["dbname"] = "postgres"
    return conninfo.make_conninfo(**params)


def _test_dbname() -> str:
    name = conninfo.conninfo_to_dict(TEST_DATABASE_URL).get("dbname")
    if not name:
        raise RuntimeError(f"no database name in AUTOTRAIN_TEST_DATABASE_URL: {TEST_DATABASE_URL}")
    return str(name)


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """A freshly created database with every migration applied, once per run."""
    dbname = _test_dbname()
    with psycopg.connect(_maintenance_conninfo(), autocommit=True) as admin:
        # Identifiers cannot be parameterised, so this is the one place SQL is
        # composed — via psycopg's Identifier quoting, never string formatting.
        ident = sql.Identifier(dbname)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(ident))

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        migrate_up(conn)

    yield TEST_DATABASE_URL


@pytest.fixture
def conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back at the end of the test.

    Repositories take a connection as their first argument, so a test can pass
    this one and leave the database untouched for the next test.
    """
    with psycopg.connect(migrated_database) as connection:
        yield connection
        connection.rollback()


@pytest.fixture
def pool(migrated_database: str) -> Iterator[None]:
    """For code paths that go through `db.transaction()` rather than taking a
    connection. These do commit, so such tests must clean up after themselves."""
    db.init_pool()
    yield
    db.close_pool()
