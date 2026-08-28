"""Schema tests: prove the constraints that the system's guarantees depend on.

Each test here corresponds to a guarantee named in the migration comments. If
one of these fails after a schema change, a product invariant broke — not a test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from conftest import mk_user as _mk_user
from conftest import scalar as _scalar


def _mk_ticket(conn: psycopg.Connection, user_id: str) -> str:
    return _scalar(
        conn.execute(
            "INSERT INTO tickets (user_id, kind, price_pence, source) "
            "VALUES (%s, 'single', 4550, 'manual') RETURNING id",
            (user_id,),
        )
    )


def _mk_journey(conn: psycopg.Connection, user_id: str, ticket_id: str) -> str:
    dep = datetime(2026, 8, 10, 8, 14, tzinfo=UTC)
    return _scalar(
        conn.execute(
            "INSERT INTO journeys (user_id, ticket_id, origin_crs, destination_crs, "
            "travel_date, scheduled_departure, scheduled_arrival) "
            "VALUES (%s, %s, 'MAN', 'EUS', %s, %s, %s) RETURNING id",
            (user_id, ticket_id, date(2026, 8, 10), dep, dep + timedelta(hours=2)),
        )
    )


class TestMigrationsApply:
    def test_all_expected_tables_exist(self, conn: psycopg.Connection) -> None:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        tables = {r[0] for r in rows}
        expected = {
            "operators",
            "delay_repay_bands",
            "users",
            "devices",
            "tickets",
            "journeys",
            "delay_detections",
            "claims",
            "claim_events",
            "claim_proofs",
            "adapter_runs",
            "schema_migrations",
        }
        assert expected <= tables, f"missing: {expected - tables}"

    def test_launch_operators_file_by_deep_link(self, conn: psycopg.Connection) -> None:
        """0011's payload. The UPDATE ... FROM (VALUES ...) join silently
        matches zero rows on a mistyped ATOC code — the suite would stay green
        while that operator's users get 'no filing link' forever. This pins
        exactly WHO got the adapter; 0011's CHECK already guarantees each has
        a URL."""
        rows = conn.execute(
            "SELECT atoc_code, claim_url FROM operators WHERE adapter = 'deep_link'"
        ).fetchall()
        assert {r[0] for r in rows} == {"NT", "VT", "GR", "SW", "TL", "GN", "SN", "SE", "GW"}
        assert all(r[1] for r in rows)

    def test_operators_seeded_with_bands(self, conn: psycopg.Connection) -> None:
        n_ops = _scalar(conn.execute("SELECT count(*) FROM operators"))
        assert n_ops >= 20
        # Every operator has a scale, and every scale starts at its threshold.
        orphans = conn.execute(
            "SELECT o.atoc_code FROM operators o "
            "LEFT JOIN delay_repay_bands b "
            "  ON b.operator_id = o.id AND b.min_minutes = o.min_delay_minutes "
            "WHERE b.id IS NULL"
        ).fetchall()
        assert orphans == []


class TestIdempotencyGuarantees:
    """The three guarantees from 0006's header, each backed by a constraint."""

    def test_one_detection_per_journey(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))
        ins = (
            "INSERT INTO delay_detections "
            "(journey_id, actual_arrival, delay_minutes, source, entitlement_pence) "
            "VALUES (%s, now(), 35, %s, 2275) "
            "ON CONFLICT (journey_id) DO NOTHING"
        )
        first = conn.execute(ins, (jid, "darwin"))
        second = conn.execute(ins, (jid, "hsp"))  # the sweep arrives later
        assert first.rowcount == 1
        assert second.rowcount == 0  # caller sees 0 -> does not notify again

    def test_one_claim_per_journey(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))
        det = _scalar(
            conn.execute(
                "INSERT INTO delay_detections "
                "(journey_id, actual_arrival, delay_minutes, source, entitlement_pence) "
                "VALUES (%s, now(), 35, 'darwin', 2275) RETURNING id",
                (jid,),
            )
        )
        op = _scalar(conn.execute("SELECT id FROM operators LIMIT 1"))
        ins = (
            "INSERT INTO claims "
            "(journey_id, detection_id, operator_id, user_id, amount_pence, file_by) "
            "VALUES (%s, %s, %s, %s, 2275, %s) "
            "ON CONFLICT (journey_id) DO NOTHING"
        )
        args = (jid, det, op, uid, date(2026, 9, 7))
        assert conn.execute(ins, args).rowcount == 1
        assert conn.execute(ins, args).rowcount == 0

    def test_duplicate_journey_add_is_rejected(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        tid = _mk_ticket(conn, uid)
        _mk_journey(conn, uid, tid)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _mk_journey(conn, uid, tid)


class TestConstraintsRejectBadData:
    def test_money_must_be_positive_pence(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO tickets (user_id, price_pence, source) VALUES (%s, -100, 'manual')",
                (uid,),
            )

    def test_crs_codes_are_validated(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        tid = _mk_ticket(conn, uid)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO journeys (user_id, ticket_id, origin_crs, "
                "destination_crs, travel_date, scheduled_departure, scheduled_arrival) "
                "VALUES (%s, %s, 'Manchester', 'EUS', '2026-08-10', now(), now() + interval '2h')",
                (uid, tid),
            )

    def test_email_is_case_insensitive(self, conn: psycopg.Connection) -> None:
        _mk_user(conn, "Bob@Example.com")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _mk_user(conn, "bob@example.com")

    def test_arrival_must_follow_departure(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn)
        tid = _mk_ticket(conn, uid)
        dep = datetime(2026, 8, 10, 8, 14, tzinfo=UTC)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO journeys (user_id, ticket_id, origin_crs, "
                "destination_crs, travel_date, scheduled_departure, scheduled_arrival) "
                "VALUES (%s, %s, 'MAN', 'EUS', '2026-08-10', %s, %s)",
                (uid, tid, dep, dep - timedelta(minutes=5)),
            )


class TestGdprErasure:
    def test_erased_email_is_reusable_but_claims_survive(self, conn: psycopg.Connection) -> None:
        uid = _mk_user(conn, "leaver@example.com")
        jid = _mk_journey(conn, uid, _mk_ticket(conn, uid))
        det = _scalar(
            conn.execute(
                "INSERT INTO delay_detections "
                "(journey_id, actual_arrival, delay_minutes, source, entitlement_pence) "
                "VALUES (%s, now(), 35, 'darwin', 2275) RETURNING id",
                (jid,),
            )
        )
        op = _scalar(conn.execute("SELECT id FROM operators LIMIT 1"))
        conn.execute(
            "INSERT INTO claims "
            "(journey_id, detection_id, operator_id, user_id, amount_pence, file_by) "
            "VALUES (%s, %s, %s, %s, 2275, '2026-09-07')",
            (jid, det, op, uid),
        )

        # Anonymise: null PII, stamp deleted_at (per the users_email_presence CHECK).
        conn.execute(
            "UPDATE users SET email = NULL, password_hash = NULL, "
            "display_name = NULL, deleted_at = now() WHERE id = %s",
            (uid,),
        )
        # The email is immediately reusable by a new account...
        _mk_user(conn, "leaver@example.com")
        # ...and the filed claim is still there for audit.
        n = _scalar(conn.execute("SELECT count(*) FROM claims WHERE user_id = %s", (uid,)))
        assert n == 1

    def test_live_user_without_email_is_impossible(self, conn: psycopg.Connection) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO users (email) VALUES (NULL)")
