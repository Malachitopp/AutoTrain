from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from autotrain.core.config import get_settings
from autotrain.core.migrate import (
    Migration,
    MigrationError,
    _verify_unchanged,
    discover,
    migrate_up,
)


def _write(directory: Path, name: str, body: str = "SELECT 1;\n") -> None:
    (directory / name).write_text(body, encoding="utf-8")


class TestDiscover:
    def test_orders_by_version_not_filename(self, tmp_path: Path) -> None:
        _write(tmp_path, "0010_later.sql")
        _write(tmp_path, "0002_earlier.sql")
        assert [m.version for m in discover(tmp_path)] == [2, 10]

    def test_rejects_unparseable_filename(self, tmp_path: Path) -> None:
        _write(tmp_path, "add_users.sql")
        with pytest.raises(MigrationError, match="NNNN_snake_case"):
            discover(tmp_path)

    def test_rejects_duplicate_version(self, tmp_path: Path) -> None:
        _write(tmp_path, "0003_one.sql")
        _write(tmp_path, "0003_two.sql")
        with pytest.raises(MigrationError, match="duplicate migration version"):
            discover(tmp_path)

    def test_rejects_empty_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_nothing.sql", "\n\n")
        with pytest.raises(MigrationError, match="empty"):
            discover(tmp_path)

    def test_no_transaction_marker_is_detected(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_normal.sql")
        _write(
            tmp_path,
            "0002_concurrent.sql",
            "-- migrate:no-transaction\nCREATE INDEX CONCURRENTLY x ON t (c);\n",
        )
        normal, concurrent = discover(tmp_path)
        assert normal.in_transaction is True
        assert concurrent.in_transaction is False

    def test_checksum_changes_with_content(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_a.sql", "SELECT 1;\n")
        first = discover(tmp_path)[0].checksum
        _write(tmp_path, "0001_a.sql", "SELECT 2;\n")
        assert discover(tmp_path)[0].checksum != first


class TestImmutability:
    """An applied migration whose file has changed means the schema in the
    database and the schema described by the repo have silently diverged."""

    def _migration(self, checksum: str) -> Migration:
        return Migration(
            version=1,
            name="init",
            path=Path("0001_init.sql"),
            sql="SELECT 1;",
            checksum=checksum,
            in_transaction=True,
        )

    def test_unchanged_file_passes(self) -> None:
        _verify_unchanged([self._migration("abc")], {1: "abc"})

    def test_edited_file_is_rejected(self) -> None:
        with pytest.raises(MigrationError, match="modified since it was applied"):
            _verify_unchanged([self._migration("def")], {1: "abc"})

    def test_unapplied_file_is_ignored(self) -> None:
        _verify_unchanged([self._migration("abc")], {})


class TestAgainstRealPostgres:
    def test_every_migration_is_recorded(self, conn: psycopg.Connection) -> None:
        expected = {m.version for m in discover(get_settings().migrations_dir)}
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            assert {row[0] for row in cur.fetchall()} == expected

    def test_rerunning_applies_nothing(self, migrated_database: str) -> None:
        with psycopg.connect(migrated_database, autocommit=True) as conn:
            assert migrate_up(conn) == []

    def test_citext_extension_is_installed(self, conn: psycopg.Connection) -> None:
        """0001 is only worth having if it actually took effect."""
        with conn.cursor() as cur:
            cur.execute("SELECT 'A'::citext = 'a'::citext")
            row = cur.fetchone()
        assert row is not None
        assert row[0] is True
