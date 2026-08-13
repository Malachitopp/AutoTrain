"""Migration runner.

Migrations are hand-written SQL files in `backend/migrations/`, named
`NNNN_snake_case.sql`. They are applied in numeric order, exactly once, and are
never edited after being applied anywhere — the runner stores a checksum and
refuses to continue if a file it has already run has changed underneath it.

Why hand-rolled rather than Alembic: with no ORM there is no model metadata to
diff against, so an autogenerating tool has nothing to autogenerate. What remains
is "run these files in order, once" — about 150 lines, and worth owning outright
because every deploy runs it.

Two details that matter in production:

* **Advisory lock.** ECS starts several tasks at once and each runs migrations at
  boot. `pg_advisory_lock` serialises them: the first applies, the rest block,
  wake up, find nothing pending and carry on.
* **Escape hatch for non-transactional DDL.** `CREATE INDEX CONCURRENTLY` cannot
  run inside a transaction, and it is the only way to add an index to a hot table
  without locking writes. A migration whose first line is `-- migrate:no-transaction`
  runs outside one. Such a migration must contain a single statement, because a
  failure halfway through leaves no rollback.

Usage:
    python -m autotrain.core.migrate up
    python -m autotrain.core.migrate status
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString, cast

import psycopg

from autotrain.core.config import get_settings

logger = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
NO_TRANSACTION_MARKER = "-- migrate:no-transaction"

# Arbitrary but fixed: any process taking this lock is running AutoTrain migrations.
ADVISORY_LOCK_KEY = 0x4155544F  # b"AUTO"

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str
    in_transaction: bool

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(migrations_dir: Path) -> list[Migration]:
    """Read and validate every migration file, ordered by version."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    found: dict[int, Migration] = {}
    for path in sorted(migrations_dir.glob("*.sql")): #any file that ends in .sql
        match = FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name}: migration files must be named NNNN_snake_case.sql"
            )
        version, name = int(match.group(1)), match.group(2)
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: "
                f"{found[version].path.name} and {path.name}"
            )
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError(f"{path.name}: migration is empty")
        found[version] = Migration(
            version=version,
            name=name,
            path=path,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            in_transaction=not sql.lstrip().startswith(NO_TRANSACTION_MARKER),
        )
    return [found[v] for v in sorted(found)]


def applied_checksums(conn: psycopg.Connection) -> dict[int, str]:
    conn.execute(LEDGER_DDL)
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return dict(cur.fetchall())


def _verify_unchanged(migrations: list[Migration], applied: dict[int, str]) -> None:
    """An applied migration whose file has changed means the code and the database
    now disagree about what the schema is. There is no safe automatic recovery."""
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationError(
                f"{migration.path.name} has been modified since it was applied. "
                "Applied migrations are immutable — revert the file and add a new "
                "migration instead."
            )


@contextmanager
def _advisory_lock(conn: psycopg.Connection) -> Iterator[None]:
    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def _record(conn: psycopg.Connection, migration: Migration, duration_ms: int) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum, duration_ms) "
        "VALUES (%s, %s, %s, %s)",
        (migration.version, migration.name, migration.checksum, duration_ms),
    )


def _apply(conn: psycopg.Connection, migration: Migration) -> None:
    started = time.monotonic()

    # The only place in the codebase where SQL is not a `LiteralString`. Migration
    # text is read from a file at runtime, so the type system cannot prove it came
    # from source. It is safe for a different reason: the files ship inside our own
    # image, are checksummed against the ledger, and never contain user input. Every
    # other query goes through `core.db`, which will not accept a runtime string.
    sql = cast(LiteralString, migration.sql)

    if migration.in_transaction:
        with conn.transaction():
            conn.execute(sql)
            _record(conn, migration, int((time.monotonic() - started) * 1000))
    else:
        # Outside a transaction there is no atomicity between the DDL and the
        # ledger row. If the process dies in between, the migration has run but
        # is not recorded, and the next run will attempt it again.
        conn.execute(sql)
        _record(conn, migration, int((time.monotonic() - started) * 1000))


def migrate_up(conn: psycopg.Connection) -> list[Migration]:
    """Apply every pending migration. Returns the ones that ran."""
    migrations = discover(get_settings().migrations_dir)
    ran: list[Migration] = []

    with _advisory_lock(conn):
        # Re-read inside the lock: another task may have applied migrations while
        # we were waiting for it.
        applied = applied_checksums(conn)
        _verify_unchanged(migrations, applied)

        for migration in migrations:
            if migration.version in applied:
                continue
            logger.info("applying %s", migration.label)
            _apply(conn, migration)
            ran.append(migration)

    return ran


def connect() -> psycopg.Connection:
    """A dedicated connection, not one from the pool.

    Migrations run at boot before the pool exists, hold a session-level advisory
    lock, and manage their own transaction boundaries.
    """
    return psycopg.connect(get_settings().database_url, autocommit=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrain-migrate")
    parser.add_argument("command", choices=["up", "status"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")

    try:
        with connect() as conn:
            if args.command == "up":
                ran = migrate_up(conn)
                if ran:
                    print(f"applied {len(ran)} migration(s): " + ", ".join(m.label for m in ran))
                else:
                    print("database is up to date")
            else:
                migrations = discover(get_settings().migrations_dir)
                applied = applied_checksums(conn)
                for migration in migrations:
                    mark = "applied" if migration.version in applied else "PENDING"
                    print(f"  {migration.label:<44} {mark}")
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
