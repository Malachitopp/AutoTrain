"""Mailbox poll tests: what one pass over an inbox does to the database.

The mailbox is a test double and the database is real — the same split as
every other sweep suite here. "Never mock the database" is about SQL; the
mailbox is the external transport, which is exactly the seam the Mailbox
protocol exists to fake. Every insert, unique index and cascade below is
real Postgres.

The `conn` fixture is already inside a transaction by the time a test calls
the poll (mk_user has written a row), so the `conn.transaction()` the sweep
opens per message is a savepoint and nothing here commits. commit_each is
therefore left False throughout: it is the scheduler's setting, not a
property of the poll being tested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from autotrain.modules.journeys.service import (
    MailboxAttachment,
    MailboxHeader,
    MailboxMessage,
    receive_ticket_email,
    run_mailbox_poll,
    run_retention_sweep,
)
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

TICKET_PDF = MailboxAttachment(
    filename="e-ticket.pdf", content_type="application/pdf", content=b"%PDF-1.4 ticket"
)


class _FakeMailbox:
    """A Mailbox that answers from a dict, and remembers what was asked for.

    `vanished` are messages that list but no longer fetch — someone tidying
    their inbox mid-poll. `broken` are ones whose fetch raises, which is
    what a dropped connection looks like from here.
    """

    def __init__(
        self,
        messages: list[MailboxMessage],
        *,
        vanished: set[str] = frozenset(),  # type: ignore[assignment]
        broken: set[str] = frozenset(),  # type: ignore[assignment]
    ) -> None:
        self.messages = {message.uid: message for message in messages}
        self.vanished = set(vanished)
        self.broken = set(broken)
        self.fetched: list[str] = []

    def list_recent(self, *, since: Any, limit: int) -> list[MailboxHeader]:
        self.listed_since = since
        return [
            MailboxHeader(uid=message.uid, message_id=message.message_id)
            for message in list(self.messages.values())[-limit:]
        ]

    def fetch(self, uid: str) -> MailboxMessage | None:
        self.fetched.append(uid)
        if uid in self.broken:
            raise RuntimeError("the mailbox went away mid-poll")
        if uid in self.vanished:
            return None
        return self.messages[uid]


def _message(
    uid: str,
    *,
    subject: str = "Your booking confirmation",
    attachments: tuple[MailboxAttachment, ...] = (),
) -> MailboxMessage:
    return MailboxMessage(
        uid=uid,
        message_id=f"<{uid}@retailer.example>",
        sender="noreply@retailer.example",
        recipient="traveller@example.com",
        subject=subject,
        body="<html><body><p>London Paddington 08:14</p></body></html>",
        attachments=attachments,
    )


def _attachments_of(conn: psycopg.Connection, message_id: str) -> list[tuple[str, str, int]]:
    """(filename, content_type, size_bytes) for one stored email, in order."""
    rows = conn.execute(
        "SELECT a.filename, a.content_type, a.size_bytes "
        "FROM inbound_email_attachments a JOIN inbound_emails e ON e.id = a.inbound_email_id "
        "WHERE e.message_id = %s ORDER BY a.created_at, a.filename",
        (message_id,),
    ).fetchall()
    return [(str(name), str(kind), int(size)) for name, kind, size in rows]


@pytest.mark.usefixtures("migrated_database")
def test_a_poll_stores_what_is_new_and_never_fetches_a_body_twice(
    conn: psycopg.Connection,
) -> None:
    """One pass, every outcome a pass can have.

    Asserted together because they are one rule about one loop, and testing
    them apart would let the counting drift from the storing. The load-
    bearing claim is the last one: a message already in the database is
    never downloaded again. Without it a fortnight's window would be
    re-fetched in full every fifteen minutes for ever.
    """
    user_id = _mk_user(conn, "traveller@example.com")

    # Already stored, exactly as a previous poll would have left it.
    seen = _message("11")
    receive_ticket_email(
        conn,
        user_id=user_id,
        account_email="traveller@example.com",
        message_id=seen.message_id,
        sender=seen.sender,
        recipient=seen.recipient,
        subject=seen.subject,
        body=seen.body,
    )

    fresh = _message("12", attachments=(TICKET_PDF,))
    gone = _message("13")
    broken = _message("14")
    mailbox = _FakeMailbox([seen, fresh, gone, broken], vanished={gone.uid}, broken={broken.uid})

    stats = run_mailbox_poll(
        conn,
        mailbox,
        user_id=user_id,
        account_email="traveller@example.com",
        lookback_days=14,
    )

    assert (stats.listed, stats.fresh) == (4, 3)
    assert (stats.stored, stats.files) == (1, 1)
    assert (stats.vanished, stats.errors) == (1, 1)
    # The one already in the database was never asked for.
    assert seen.uid not in mailbox.fetched
    assert sorted(mailbox.fetched) == ["12", "13", "14"]
    # The window asked for is the one configured, counted back from today.
    assert mailbox.listed_since == (datetime.now(UTC) - timedelta(days=14)).date()

    # The fresh one is queued for the reader, with its ticket beside it.
    assert (
        _scalar(
            conn.execute(
                "SELECT status FROM inbound_emails WHERE message_id = %s", (fresh.message_id,)
            )
        )
        == "received"
    )
    assert _attachments_of(conn, fresh.message_id) == [
        ("e-ticket.pdf", "application/pdf", len(TICKET_PDF.content))
    ]
    # The one that raised wrote nothing — though it raised before any
    # statement, so this only says the counting is right. The savepoint
    # itself is what the next test is for.
    assert (
        conn.execute(
            "SELECT id FROM inbound_emails WHERE message_id = %s", (broken.message_id,)
        ).fetchone()
        is None
    )


@pytest.mark.usefixtures("migrated_database")
def test_a_message_that_fails_half_way_leaves_no_row_behind(conn: psycopg.Connection) -> None:
    """The savepoint, tested where it can actually be observed.

    An email is two writes: the row, then its attachments. A failure between
    them is the case worth proving, because a surviving email row would have
    its message_id stored — and a stored message_id is never offered again,
    so the ticket would be permanently lost while the poll reported success.

    A NUL in the filename is the real trigger. Postgres text cannot hold one,
    so psycopg refuses the parameter at the second write. The IMAP source
    strips control characters precisely so this never comes off a real
    mailbox (sources/imap_mailbox.py, _clean); the fake here hands one
    straight to the poll, which is the only way to reach the failure.
    """
    user_id = _mk_user(conn, "traveller@example.com")
    message = _message(
        "41",
        attachments=(
            MailboxAttachment(
                filename="ticket\x00.pdf", content_type="application/pdf", content=b"%PDF-1.4"
            ),
        ),
    )

    stats = run_mailbox_poll(
        conn,
        _FakeMailbox([message]),
        user_id=user_id,
        account_email="traveller@example.com",
    )

    assert (stats.errors, stats.stored) == (1, 0)
    # Neither half survived, so the next poll will offer it again.
    assert (
        conn.execute(
            "SELECT id FROM inbound_emails WHERE message_id = %s", (message.message_id,)
        ).fetchone()
        is None
    )
    assert _attachments_of(conn, message.message_id) == []


@pytest.mark.usefixtures("migrated_database")
def test_only_files_that_could_be_a_ticket_are_kept(conn: psycopg.Connection) -> None:
    """The store is for evidence a claim can be filed with, not for whatever
    a marketing email hangs off itself.

    Three ways in: the declared type must be one a ticket comes as, the file
    must be small enough to be a ticket, and there is a ceiling on how many
    are kept. The excess is dropped silently rather than refusing the email
    — the words are what the reader needs, and an email that arrived with a
    40 MB video must still become a journey.
    """
    user_id = _mk_user(conn, "traveller@example.com")
    oversized = MailboxAttachment(
        filename="video.pdf", content_type="application/pdf", content=b"x" * 5_000_001
    )
    logo = MailboxAttachment(filename="logo.svg", content_type="image/svg+xml", content=b"<svg/>")
    empty = MailboxAttachment(filename="nothing.pdf", content_type="application/pdf", content=b"")
    # Six keepable files; only five may be stored, earliest first.
    many = tuple(
        MailboxAttachment(
            filename=f"leg-{index}.png", content_type="image/png", content=b"PNG" + bytes([index])
        )
        for index in range(6)
    )
    message = _message("21", attachments=(oversized, logo, empty, *many))

    stats = run_mailbox_poll(
        conn,
        _FakeMailbox([message]),
        user_id=user_id,
        account_email="traveller@example.com",
    )

    assert (stats.stored, stats.files) == (1, 5)
    kept = _attachments_of(conn, message.message_id)
    assert [name for name, _, _ in kept] == [f"leg-{index}.png" for index in range(5)]
    assert {kind for _, kind, _ in kept} == {"image/png"}


@pytest.mark.usefixtures("migrated_database")
def test_a_backlog_bigger_than_one_pass_drains_instead_of_starving(
    conn: psycopg.Connection,
) -> None:
    """The per-pass limit bounds BODIES fetched, never what can be seen.

    The bug this exists for: capping the LISTING first meant that once the
    limit's worth of messages in the window were already stored, a pass had
    nothing left to look at, and the older unimported ones behind them could
    never be reached. They sat there until they aged out of the lookback
    window — permanently lost — while every pass logged a clean zero. It bites
    the ordinary case of labelling a year of bookings in one sitting.

    Oldest first, so each pass makes progress on the back of the queue rather
    than re-examining a fixed head of it.
    """
    user_id = _mk_user(conn, "traveller@example.com")
    messages = [_message(str(50 + index)) for index in range(5)]
    mailbox = _FakeMailbox(messages)

    def poll() -> Any:
        return run_mailbox_poll(
            conn,
            mailbox,
            user_id=user_id,
            account_email="traveller@example.com",
            limit=2,
        )

    first, second, third = poll(), poll(), poll()

    assert [s.stored for s in (first, second, third)] == [2, 2, 1]
    # Every pass could SEE all five; only the fetching was rationed.
    assert [s.listed for s in (first, second, third)] == [5, 5, 5]
    assert [s.fresh for s in (first, second, third)] == [5, 3, 1]
    # Oldest first, and no body was ever fetched twice.
    assert mailbox.fetched == ["50", "51", "52", "53", "54"]
    assert (
        _scalar(conn.execute("SELECT count(*) FROM inbound_emails WHERE user_id = %s", (user_id,)))
        == 5
    )
    # A fourth pass has nothing left to do.
    assert poll().stored == 0


@pytest.mark.usefixtures("migrated_database")
def test_retention_takes_the_attachments_with_the_body(conn: psycopg.Connection) -> None:
    """A file has to die with the words that say what it is.

    An e-ticket carries a name and a booking reference. Once the body it
    arrived with has been blanked there is nothing left saying which journey
    it belonged to or why it was kept, so keeping the file would be holding
    the most identifying thing in the system with the least justification.
    """
    user_id = _mk_user(conn, "traveller@example.com")
    message = _message("31", attachments=(TICKET_PDF,))
    run_mailbox_poll(
        conn,
        _FakeMailbox([message]),
        user_id=user_id,
        account_email="traveller@example.com",
    )
    email_id: UUID = _scalar(
        conn.execute("SELECT id FROM inbound_emails WHERE message_id = %s", (message.message_id,))
    )
    # Decided long enough ago to be past any retention window.
    conn.execute(
        "UPDATE inbound_emails SET status = 'parsed', processed_at = now() - interval '90 days' "
        "WHERE id = %s",
        (email_id,),
    )
    assert _attachments_of(conn, message.message_id) != []

    assert run_retention_sweep(conn, keep_days=60) == 1

    body, purged = conn.execute(
        "SELECT body, body_purged_at FROM inbound_emails WHERE id = %s", (email_id,)
    ).fetchone() or ("unread", None)
    assert body == ""
    assert purged is not None
    assert _attachments_of(conn, message.message_id) == []
