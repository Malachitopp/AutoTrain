"""core.db's job lock: the guarantee the sweeps' one-process-at-a-time
assumption rests on. Two real connections play two processes."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors as pg_errors

from autotrain.core import db


def test_job_lock_is_exclusive_per_name_and_always_released(migrated_database: str) -> None:
    with (
        psycopg.connect(migrated_database) as first,
        psycopg.connect(migrated_database) as second,
    ):
        with db.job_lock(first, "claim-sweep") as held:
            assert held is True
            # The other process finds it held and does not wait.
            with db.job_lock(second, "claim-sweep") as other:
                assert other is False
            # Names are separate locks.
            with db.job_lock(second, "ticket-intake") as other:
                assert other is True
            # Held across the holder's commits: a sweep commits as it goes.
            first.commit()
            with db.job_lock(second, "claim-sweep") as other:
                assert other is False
        with db.job_lock(second, "claim-sweep") as other:
            assert other is True

        # A unit that dies mid-transaction still releases the lock — an
        # aborted transaction cannot run the unlock, so the lock ends it
        # first — and the failure still reaches the caller.
        with pytest.raises(pg_errors.DivisionByZero), db.job_lock(first, "claim-sweep") as held:
            assert held is True
            first.execute("SELECT 1/0")
        with db.job_lock(second, "claim-sweep") as other:
            assert other is True
