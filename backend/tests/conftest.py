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
from typing import Any

import psycopg
import pytest
from psycopg import conninfo, sql

DEFAULT_TEST_URL = "postgresql://autotrain:autotrain@localhost:5433/autotrain_test"

# The suite drops and recreates its database, so it must run against the test URL
# and nothing else. Set this before importing anything that reads settings.
TEST_DATABASE_URL = os.environ.get("AUTOTRAIN_TEST_DATABASE_URL", DEFAULT_TEST_URL)
os.environ["AUTOTRAIN_DATABASE_URL"] = TEST_DATABASE_URL

# Session tokens are minted and verified with this fixed secret — 32+ bytes,
# the RFC 7518 floor pyjwt warns below. Forced (not setdefault) so a
# developer's .env can never leak into what the suite signs with; set before
# any autotrain import so the cached Settings sees it. email_sender is pinned
# to 'none' for the same reason: the 503 refusal path must be the suite's
# reality regardless of what the developer's .env turned on.
TEST_JWT_SECRET = "test-secret-not-for-production-padded-to-32-bytes"
os.environ["AUTOTRAIN_JWT_SECRET"] = TEST_JWT_SECRET
os.environ["AUTOTRAIN_EMAIL_SENDER"] = "none"
# Where magic links point. Pinned so the link format the auth suites assert
# on is deterministic; the missing-config 503 path patches it away per test.
TEST_APP_BASE_URL = "http://frontend.test"
os.environ["AUTOTRAIN_APP_BASE_URL"] = TEST_APP_BASE_URL

from autotrain.core import db  # noqa: E402
from autotrain.core.config import get_settings  # noqa: E402
from autotrain.core.migrate import migrate_up  # noqa: E402
from autotrain.modules.identity import service as identity  # noqa: E402

get_settings.cache_clear()


def _maintenance_conninfo() -> str:
    """Connection string for the `postgres` database — you cannot drop the
    database you are currently connected to.

    Host, port and credentials are inherited from the test URL; only the target
    database is overridden.
    """
    return conninfo.make_conninfo(TEST_DATABASE_URL, dbname="postgres")


def _test_dbname() -> str:
    """The database this suite is allowed to destroy.

    `conninfo_to_dict` returns `str | int | None` values, so the name is narrowed
    to a string before use.
    """
    raw = conninfo.conninfo_to_dict(TEST_DATABASE_URL).get("dbname")
    if not raw:
        raise RuntimeError(f"no database name in AUTOTRAIN_TEST_DATABASE_URL: {TEST_DATABASE_URL}")

    name = str(raw)
    # This fixture runs DROP DATABASE ... WITH (FORCE). A mistyped environment
    # variable should fail the run, not destroy whatever it happens to point at.
    if not name.endswith("_test"):
        raise RuntimeError(
            f"refusing to run: test database {name!r} must end in '_test'. "
            "This suite drops and recreates the database it connects to."
        )
    return name


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


# --- Shared row helpers ------------------------------------------------------
# One definition each; test files import and alias these. Promoted here after
# the fourth verbatim copy appeared (PR #6 review).


def scalar(cur: psycopg.Cursor[Any]) -> Any:
    """First column of a row that must exist (RETURNING id, count(*), ...).

    `fetchone()` is typed `tuple | None`; the assert narrows away the None arm
    for queries whose shape guarantees a row — which the checker can't know.
    """
    row = cur.fetchone()
    assert row is not None, "query was expected to return a row"
    return row[0]


def mk_user(conn: psycopg.Connection, email: str = "user@example.com") -> Any:
    """A consented user row — the row everything else hangs off."""
    return scalar(
        conn.execute(
            "INSERT INTO users (email, claim_consent_at, claim_consent_terms) "
            "VALUES (%s, now(), 'loa-v1') RETURNING id",
            (email,),
        )
    )


def auth_header(user_id: Any) -> dict[str, str]:
    """Authorization header for user_id — a real JWT minted with the suite's
    fixed secret, so API tests authenticate exactly the way production does
    (signature and expiry checked per request, no dependency overrides)."""
    token = identity.issue_session_token(user_id, secret=TEST_JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}
