"""Erasure (GDPR): after identity.erase_user nothing that identifies the
person remains anywhere, while the claims filed on their behalf stay for
audit under an id that names nobody.

One user with one of everything, then a scan of EVERY text column in the
database for the strings that identify them. That scan is the point: a
future migration that adds a place personal data lives fails this test
until erasure learns about it, instead of quietly leaving the data behind.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import psycopg
from psycopg import sql

from autotrain.modules.identity import service as identity
from autotrain.modules.journeys import service as journeys
from autotrain.modules.journeys.service import ExtractedLeg, ExtractedTicket
from conftest import (
    TEST_APP_BASE_URL,
    TEST_INBOUND_DOMAIN,
    TEST_JWT_SECRET,
    mk_operator,
    mk_user,
    scalar,
)

EMAIL = "leaving@example.com"
BODY = "Leaving Railways e-ticket: Leeds to London Kings Cross, booking ref ZX123QQ"
RETAILER = "Leaving Railways"
PUSH_TOKEN = "device-token-of-the-leaving-user"


class _Inbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


class _Reader:
    """A TicketExtractor that reads the one email as one single, two days ago."""

    def extract(self, *, subject: str, body: str, received_at: datetime) -> ExtractedTicket:
        day = (received_at - timedelta(days=2)).date().isoformat()
        return ExtractedTicket(
            is_ticket=True,
            retailer=RETAILER,
            kind="single",
            price_pence=6200,
            legs=[
                ExtractedLeg(
                    origin_crs="LDS",
                    destination_crs="KGX",
                    travel_date=day,
                    departure_time="09:15",
                    arrival_time="11:31",
                    operator_name=None,
                    operator_atoc_code=None,
                )
            ],
            confidence="high",
            notes=None,
        )


def _one_of_everything(conn: psycopg.Connection) -> tuple[UUID, str]:
    """A user, a pending login token, a device, a forwarding address, a
    forwarded email read into a ticket and journey, a detection and a claim."""
    user_id = mk_user(conn, EMAIL)
    identity.request_login(conn, EMAIL, _Inbox(), app_base_url=TEST_APP_BASE_URL)
    conn.execute(
        "INSERT INTO devices (user_id, platform, push_token) VALUES (%s, 'ios', %s)",
        (user_id, PUSH_TOKEN),
    )
    code = identity.forwarding_code(conn, user_id)
    assert code is not None
    address = identity.forwarding_address(code, TEST_INBOUND_DOMAIN)
    journeys.receive_ticket_email(
        conn,
        user_id=user_id,
        message_id="<leaving@retailer.test>",
        sender=EMAIL,
        recipient=address,
        subject="Your e-ticket",
        body=BODY,
    )
    assert journeys.run_intake_sweep(conn, _Reader()).parsed == 1
    journey_id = scalar(conn.execute("SELECT id FROM journeys WHERE user_id = %s", (user_id,)))
    operator_id = mk_operator(conn)
    conn.execute(
        "UPDATE journeys SET operator_id = %s, status = 'assessed' WHERE id = %s",
        (operator_id, journey_id),
    )
    detection_id = scalar(
        conn.execute(
            "INSERT INTO delay_detections (journey_id, actual_arrival, delay_minutes, source, "
            "band_percent, entitlement_pence) VALUES (%s, now(), 45, 'hsp', 50, 3100) RETURNING id",
            (journey_id,),
        )
    )
    conn.execute(
        "INSERT INTO claims (journey_id, detection_id, operator_id, user_id, amount_pence, "
        "file_by) VALUES (%s, %s, %s, %s, 3100, %s)",
        (journey_id, detection_id, operator_id, user_id, date.today() + timedelta(days=20)),
    )
    return user_id, address


def _text_columns(conn: psycopg.Connection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "AND (data_type IN ('text', 'character varying') OR udt_name = 'citext') "
        "ORDER BY table_name, column_name"
    ).fetchall()


def _columns_holding(conn: psycopg.Connection, needle: str) -> list[str]:
    """Every text column in the database with a row containing `needle`."""
    hits: list[str] = []
    for table, column in _text_columns(conn):
        query = sql.SQL("SELECT count(*) FROM {} WHERE {}::text ILIKE {}").format(
            sql.Identifier(table), sql.Identifier(column), sql.Literal(f"%{needle}%")
        )
        if scalar(conn.execute(query)):
            hits.append(f"{table}.{column}")
    return hits


def test_erase_user_leaves_nothing_of_the_person_and_keeps_the_claim(
    conn: psycopg.Connection,
) -> None:
    user_id, address = _one_of_everything(conn)
    code = address.split("@")[0].removeprefix("tickets-")
    # Before: the person is all over the database, and their session works.
    assert _columns_holding(conn, EMAIL)
    assert _columns_holding(conn, "ZX123QQ")
    assert _columns_holding(conn, code)
    token = identity.issue_session_token(
        user_id, secret=TEST_JWT_SECRET, now=datetime.now(UTC) - timedelta(seconds=5)
    )
    claims = identity.session_user(token, secret=TEST_JWT_SECRET)
    assert claims is not None
    assert identity.session_is_live(conn, user_id=claims.user_id, issued_at=claims.issued_at)

    assert identity.erase_user(conn, user_id) is True

    for needle in (EMAIL, "ZX123QQ", RETAILER, code, PUSH_TOKEN):
        assert _columns_holding(conn, needle) == [], needle
    # Every gate says no, including the session that was fine a moment ago.
    assert identity.user_is_live(conn, user_id) is False
    assert (
        identity.session_is_live(conn, user_id=claims.user_id, issued_at=claims.issued_at) is False
    )
    assert identity.user_by_forwarding_address(conn, address) is None
    assert identity.user_profile(conn, user_id) is None
    # What stays: the row (stamped), the journey and the claim, for audit.
    assert scalar(
        conn.execute("SELECT deleted_at IS NOT NULL FROM users WHERE id = %s", (user_id,))
    )
    assert scalar(conn.execute("SELECT count(*) FROM claims WHERE user_id = %s", (user_id,))) == 1
    assert scalar(conn.execute("SELECT count(*) FROM journeys WHERE user_id = %s", (user_id,))) == 1
    # Twice is a no-op, not an error.
    assert identity.erase_user(conn, user_id) is False
