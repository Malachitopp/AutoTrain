"""Ticket-email intake, service level: a forwarded email becomes journeys, or
lands somewhere a person can see why. Real Postgres via the rollback `conn`
fixture; the reader is scripted (the real one has its own tests in
test_ticket_emails_source), so every guard here is driven by a known answer.

Dates are relative to today: the intake refuses travel dates more than 60
days back, so fixed dates would start failing after a couple of months.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
import pytest

from autotrain.modules.journeys import service
from autotrain.modules.journeys.service import ExtractedLeg, ExtractedTicket, InboundEmailRow
from conftest import mk_user, scalar

RECIPIENT = "tickets-abc123def0@in.autotrain.test"
BODY = "Manchester Piccadilly to London Euston, 08:14 - 10:22. Total £45.50."

# A recent day inside the claimable window, whatever day the suite runs.
DAY = date.today() - timedelta(days=3)
DAY_AFTER = DAY + timedelta(days=2)


class ScriptedExtractor:
    """A TicketExtractor that gives one prepared answer, or raises it."""

    def __init__(self, answer: ExtractedTicket | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, datetime]] = []

    def extract(self, *, subject: str, body: str, received_at: datetime) -> ExtractedTicket:
        self.calls.append((subject, body, received_at))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def leg(**overrides: Any) -> ExtractedLeg:
    fields: dict[str, Any] = {
        "origin_crs": "MAN",
        "destination_crs": "EUS",
        "travel_date": DAY.isoformat(),
        "departure_time": "08:14",
        "arrival_time": "10:22",
        "operator_name": "Test Railways",
        "operator_atoc_code": "QQ",
    }
    fields.update(overrides)
    return ExtractedLeg(**fields)


def ticket(**overrides: Any) -> ExtractedTicket:
    fields: dict[str, Any] = {
        "is_ticket": True,
        "retailer": "Trainline",
        "kind": "single",
        "price_pence": 4550,
        "legs": [leg()],
        "confidence": "high",
        "notes": None,
    }
    fields.update(overrides)
    return ExtractedTicket(**fields)


def receive(
    conn: psycopg.Connection, user_id: UUID, message_id: str = "<one@retailer.test>"
) -> InboundEmailRow:
    row = service.receive_ticket_email(
        conn,
        user_id=user_id,
        message_id=message_id,
        sender="tickets@retailer.test",
        recipient=RECIPIENT,
        subject="Your booking",
        body=BODY,
    )
    assert row is not None
    return row


def email_state(conn: psycopg.Connection, email_id: UUID) -> tuple[str, str | None, int]:
    row = conn.execute(
        "SELECT status, status_reason, attempts FROM inbound_emails WHERE id = %s", (email_id,)
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def journeys_for(conn: psycopg.Connection, user_id: UUID) -> list[tuple]:
    return conn.execute(
        "SELECT origin_crs, destination_crs, travel_date, scheduled_departure, "
        "scheduled_arrival, operator_id, ticket_id FROM journeys "
        "WHERE user_id = %s ORDER BY scheduled_departure",
        (user_id,),
    ).fetchall()


def count(conn: psycopg.Connection, table: str, user_id: UUID) -> int:
    # Test-only SQL over three known names; the identifier never comes from input.
    assert table in ("tickets", "journeys", "inbound_emails")
    return scalar(conn.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s", (user_id,)))


def later(conn: psycopg.Connection, email: InboundEmailRow) -> None:
    """now() is fixed per transaction, so rows made in one test tie on
    received_at; this pushes one a minute later to fix an order."""
    conn.execute(
        "UPDATE inbound_emails SET received_at = received_at + interval '1 minute' WHERE id = %s",
        (email.id,),
    )


def london(day: date, hh: int, mm: int) -> datetime:
    """A London wall-clock reading as the UTC instant the database holds."""
    wall = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ZoneInfo("Europe/London"))
    return wall.astimezone(UTC)


def test_a_return_ticket_becomes_two_journeys_on_one_ticket(conn: psycopg.Connection) -> None:
    user_id = mk_user(conn)
    email = receive(conn, user_id)
    reader = ScriptedExtractor(
        ticket(
            kind="return",
            price_pence=8900,
            legs=[
                leg(),
                # Back after midnight: 23:30 out, 00:10 in — the next calendar day.
                leg(
                    origin_crs="EUS",
                    destination_crs="MAN",
                    travel_date=DAY_AFTER.isoformat(),
                    departure_time="23:30",
                    arrival_time="00:10",
                ),
            ],
        )
    )

    stats = service.run_intake_sweep(conn, reader)

    assert (stats.examined, stats.parsed, stats.errors) == (1, 1, 0)
    assert reader.calls == [("Your booking", BODY, email.received_at)]
    out, back = journeys_for(conn, user_id)
    assert (out[0], out[1], out[2]) == ("MAN", "EUS", DAY)
    assert (out[3], out[4]) == (london(DAY, 8, 14), london(DAY, 10, 22))
    assert back[3] == london(DAY_AFTER, 23, 30)
    assert back[4] == london(DAY_AFTER + timedelta(days=1), 0, 10)
    # The email named an operator; the journeys do not carry it. The seller
    # is not the operator, and the arrivals data assigns the real one.
    assert (out[5], back[5]) == (None, None)
    assert out[6] == back[6]  # one ticket, two legs
    ticket_row = conn.execute(
        "SELECT kind, price_pence, retailer, source, source_payload FROM tickets WHERE id = %s",
        (out[6],),
    ).fetchone()
    assert ticket_row == ("return", 8900, "Trainline", "email", str(email.id))

    status, reason, attempts = email_state(conn, email.id)
    assert (status, reason, attempts) == ("parsed", "2 journey(s) added", 1)
    extraction = scalar(
        conn.execute("SELECT extraction FROM inbound_emails WHERE id = %s", (email.id,))
    )
    assert extraction["kind"] == "return" and extraction["legs"][0]["operator_atoc_code"] == "QQ"
    # Decided rows leave the queue: a second sweep finds nothing.
    assert service.run_intake_sweep(conn, reader).examined == 0


@pytest.mark.parametrize(
    ("answer", "status", "reason_part"),
    [
        (ticket(is_ticket=False), "rejected", "not a ticket"),
        (ticket(kind="season"), "rejected", "season ticket"),
        (ticket(confidence="low", notes="price unclear"), "needs_review", "price unclear"),
        (ticket(kind="unknown"), "needs_review", "type unclear"),
        (ticket(price_pence=None), "needs_review", "price not found"),
        (ticket(legs=[]), "needs_review", "no journeys"),
        # One price, two trains that are not out-and-back: two singles in one
        # email, and the calculator would pay the full price on each.
        (
            ticket(legs=[leg(), leg(travel_date=DAY_AFTER.isoformat())]),
            "needs_review",
            "2 legs on a single ticket",
        ),
        (ticket(kind="return", legs=[leg(), leg()]), "needs_review", "leg 2 repeats leg 1"),
        (ticket(legs=[leg(arrival_time=None)]), "needs_review", "arrival time not found"),
        (ticket(legs=[leg(origin_crs="Manchester")]), "needs_review", "not a station code"),
        (ticket(legs=[leg(destination_crs="MAN")]), "needs_review", "same station"),
        (ticket(legs=[leg(travel_date="2026-13-40")]), "needs_review", "not a real date"),
        (ticket(legs=[leg(travel_date="2099-01-01")]), "needs_review", "too far ahead"),
        # A year misread into the past: those trains ran, HSP has data, and a
        # claim would be opened for a journey nobody took.
        (ticket(legs=[leg(travel_date="2025-01-15")]), "needs_review", "too far in the past"),
        (ticket(legs=[leg(departure_time="8am")]), "needs_review", "not HH:MM"),
        (ticket(legs=[leg(arrival_time="08:14")]), "needs_review", "equals departure"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_guard_names_its_reason_and_creates_nothing(
    conn: psycopg.Connection, answer: ExtractedTicket, status: str, reason_part: str
) -> None:
    """The deterministic half of §6a: anything the checks cannot verify is
    parked with a plain reason, and no journey is made by guesswork. The
    reader's answer is kept either way, for the person who looks."""
    user_id = mk_user(conn)
    email = receive(conn, user_id)

    stats = service.run_intake_sweep(conn, ScriptedExtractor(answer))

    got_status, reason, _ = email_state(conn, email.id)
    assert got_status == status
    assert reason is not None and reason_part in reason
    assert getattr(stats, status) == 1
    assert count(conn, "tickets", user_id) == 0
    assert scalar(
        conn.execute("SELECT extraction IS NOT NULL FROM inbound_emails WHERE id = %s", (email.id,))
    )


def test_journeys_already_tracked_are_skipped_leg_by_leg(conn: psycopg.Connection) -> None:
    """Three shapes of overlap. The same ticket forwarded twice is a
    duplicate. A return whose outbound was added by hand still gets its
    return leg tracked, and the reason says what was skipped. A repeat
    delivery of one message is not stored at all."""
    user_id = mk_user(conn)
    first = receive(conn, user_id, "<a@retailer.test>")
    second = receive(conn, user_id, "<b@retailer.test>")
    later(conn, second)

    stats = service.run_intake_sweep(conn, ScriptedExtractor(ticket()))

    assert (stats.parsed, stats.duplicate) == (1, 1)
    assert email_state(conn, first.id)[0] == "parsed"
    status, reason, _ = email_state(conn, second.id)
    assert (status, reason) == ("duplicate", "all 1 journey(s) on it were already tracked")
    # The duplicate's ticket never existed: one ticket, one journey.
    assert (count(conn, "tickets", user_id), count(conn, "journeys", user_id)) == (1, 1)

    # Now the return-ticket confirmation, whose outbound is the journey above.
    third = receive(conn, user_id, "<c@retailer.test>")
    reader = ScriptedExtractor(
        ticket(
            kind="return",
            price_pence=8900,
            legs=[
                leg(),
                leg(
                    origin_crs="EUS",
                    destination_crs="MAN",
                    travel_date=DAY_AFTER.isoformat(),
                    departure_time="17:00",
                    arrival_time="19:10",
                ),
            ],
        )
    )
    assert service.run_intake_sweep(conn, reader).parsed == 1
    status, reason, _ = email_state(conn, third.id)
    assert (status, reason) == ("parsed", "1 journey(s) added; 1 already tracked")
    assert count(conn, "journeys", user_id) == 2

    # A repeat delivery of the same message (a webhook retry) is not stored.
    assert (
        service.receive_ticket_email(
            conn,
            user_id=user_id,
            message_id="<a@retailer.test>",
            sender="tickets@retailer.test",
            recipient=RECIPIENT,
            subject="Your booking",
            body=BODY,
        )
        is None
    )
    assert count(conn, "inbound_emails", user_id) == 3


def test_a_failing_reader_waits_for_the_next_sweep_then_gives_up(
    conn: psycopg.Connection,
) -> None:
    """A reader that raises leaves the email for the NEXT sweep — never
    re-read in the same one, so one outage cannot spend every attempt at
    once — and after MAX_INTAKE_ATTEMPTS the email is marked failed. A row
    stranded 'received' with its attempts used up (a crash mid-read) is
    retired the same way, without another read."""
    user_id = mk_user(conn)
    email = receive(conn, user_id)
    reader = ScriptedExtractor(RuntimeError("model down"))

    first = service.run_intake_sweep(conn, reader)
    assert (first.examined, first.errors, first.failed) == (1, 1, 0)
    assert len(reader.calls) == 1  # once per sweep, not until exhausted
    assert email_state(conn, email.id) == ("received", None, 1)

    service.run_intake_sweep(conn, reader)
    third = service.run_intake_sweep(conn, reader)
    assert (third.errors, third.failed) == (1, 1)
    status, reason, attempts = email_state(conn, email.id)
    assert (status, attempts) == ("failed", service.MAX_INTAKE_ATTEMPTS)
    assert reason is not None and "RuntimeError" in reason
    assert len(reader.calls) == service.MAX_INTAKE_ATTEMPTS

    stranded = receive(conn, user_id, "<stranded@retailer.test>")
    conn.execute(
        "UPDATE inbound_emails SET attempts = %s WHERE id = %s",
        (service.MAX_INTAKE_ATTEMPTS, stranded.id),
    )
    stats = service.run_intake_sweep(conn, reader)
    assert (stats.examined, stats.failed, stats.errors) == (1, 1, 0)
    assert email_state(conn, stranded.id)[0] == "failed"
    assert len(reader.calls) == service.MAX_INTAKE_ATTEMPTS  # not read again

    assert service.run_intake_sweep(conn, reader).examined == 0


def test_an_email_decided_by_someone_else_is_not_read_and_leaves_no_journeys(
    conn: psycopg.Connection,
) -> None:
    """The lost-race protocol. A row decided between reading and writing
    keeps the other verdict and gets no journeys; a row decided before the
    sweep reaches it is never read at all."""
    user_id = mk_user(conn)
    email = receive(conn, user_id)

    class _DecidesMeanwhile(ScriptedExtractor):
        # Plays a reviewer who rejects the row while the model is reading it.
        def extract(self, *, subject: str, body: str, received_at: datetime) -> ExtractedTicket:
            conn.execute(
                "UPDATE inbound_emails SET status = 'rejected', status_reason = 'reviewer', "
                "processed_at = now() WHERE id = %s",
                (email.id,),
            )
            return super().extract(subject=subject, body=body, received_at=received_at)

    stats = service.run_intake_sweep(conn, _DecidesMeanwhile(ticket()))

    assert (stats.examined, stats.lost_race, stats.parsed) == (1, 1, 0)
    assert email_state(conn, email.id) == ("rejected", "reviewer", 1)
    assert count(conn, "journeys", user_id) == 0

    other = receive(conn, user_id, "<decided@retailer.test>")
    conn.execute(
        "UPDATE inbound_emails SET status = 'needs_review', processed_at = now() WHERE id = %s",
        (other.id,),
    )
    reader = ScriptedExtractor(ticket())
    assert service.run_intake_sweep(conn, reader).examined == 0
    assert reader.calls == []


def test_mail_for_nobody_records_only_that_it_arrived(conn: psycopg.Connection) -> None:
    """A code no live account has — most often an erased user's forwarding
    rule still running. There is no owner left to erase for, so nothing of
    the person is kept: not the body, not the sender, not the subject."""
    row = service.receive_ticket_email(
        conn,
        user_id=None,
        message_id="<stranger@retailer.test>",
        sender="erased-person@example.com",
        recipient="tickets-0000000000@in.autotrain.test",
        subject="Your booking ABC123",
        body="someone else's ticket",
    )
    assert row is not None
    assert (row.user_id, row.status, row.status_reason) == (None, "rejected", "unknown recipient")
    assert (row.sender, row.subject, row.body) == ("", "", "")
    assert row.recipient == "tickets-0000000000@in.autotrain.test"
    assert row.processed_at is not None
    assert service.run_intake_sweep(conn, ScriptedExtractor(ticket())).examined == 0


def test_list_inbound_emails_is_the_users_own_newest_first(conn: psycopg.Connection) -> None:
    alice = mk_user(conn, "alice@example.com")
    bob = mk_user(conn, "bob@example.com")
    older = receive(conn, alice, "<1@retailer.test>")
    newer = receive(conn, alice, "<2@retailer.test>")
    later(conn, newer)
    receive(conn, bob, "<3@retailer.test>")

    rows = service.list_inbound_emails(conn, alice)

    assert [row.id for row in rows] == [newer.id, older.id]
