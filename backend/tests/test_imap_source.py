"""IMAP source tests: the wire protocol, and what comes back off it.

No database here — this file is the transport, and the transport's whole job
is turning imaplib's answers into the shapes journeys already understands.
The fake below answers the way a server does, literals and closing brackets
included, because that shape is exactly what the parsing has to survive.

The messages are built with the stdlib's own email package rather than typed
out as bytes, so the base64, the quoted-printable and the encoded-word
subject are real ones and the decoding is genuinely exercised.
"""

from __future__ import annotations

import email.message
import imaplib
from datetime import date
from typing import Any, cast

import pytest

from autotrain.sources import imap_mailbox
from autotrain.sources.imap_mailbox import ImapMailbox

UID_VALIDITY = "998877"


def _booking_email() -> bytes:
    """A booking confirmation as a retailer really sends one: a plain-text
    stub, the real thing in HTML, and the e-ticket attached."""
    message = email.message.EmailMessage()
    message["Message-ID"] = "<booking-42@retailer.example>"
    message["From"] = "Retailer Bookings <noreply@retailer.example>"
    message["To"] = "traveller@example.com"
    # An encoded-word subject: non-ASCII in a header is normal in real mail.
    message["Subject"] = "Your booking — £24.50"
    message.set_content("View this email in a browser.")
    message.add_alternative(
        "<html><body><p>London Paddington 08:14</p></body></html>", subtype="html"
    )
    message.add_attachment(
        b"%PDF-1.4 ticket",
        maintype="application",
        subtype="pdf",
        # A path, not a name. The sender chooses this and it ends up on a
        # screen, so the directory part has to come off.
        filename="C:\\Users\\someone\\Desktop\\e-ticket.pdf",
    )
    return message.as_bytes()


def _bare_email() -> bytes:
    """A message with no Message-ID at all — common enough, and the database
    needs a unique key for every row regardless."""
    message = email.message.EmailMessage()
    message["From"] = "someone@example.com"
    message["Subject"] = "No id here"
    message.set_content("Plain words only.")
    return message.as_bytes()


class _FakeImap:
    """An IMAP server that answers from a dict of uid -> raw message.

    Every command is recorded, because two of the promises this source makes
    — read-only, and never a body twice — are promises about which commands
    it sends, not about what it returns.
    """

    def __init__(self, messages: dict[str, bytes], *, sizes: dict[str, int] | None = None) -> None:
        self.messages = messages
        self.sizes = sizes or {uid: len(raw) for uid, raw in messages.items()}
        self.commands: list[tuple[str, ...]] = []
        self.selected: tuple[str, bool] | None = None
        self.logged_out = False

    # --- the parts ImapMailbox.connect uses ------------------------------

    def login(self, username: str, password: str) -> None:
        self.commands.append(("LOGIN", username))

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> Any:
        self.selected = (mailbox, readonly)
        return ("OK", [b"3"])

    def response(self, name: str) -> Any:
        return (name, [UID_VALIDITY.encode()]) if name == "UIDVALIDITY" else (name, [None])

    def logout(self) -> Any:
        self.logged_out = True
        return ("BYE", [b"logged out"])

    # --- the command channel ---------------------------------------------

    def uid(self, command: str, *args: str) -> Any:
        self.commands.append((command, *args))
        if command == "SEARCH":
            return ("OK", [" ".join(self.messages).encode()])
        if command == "FETCH":
            uids = args[0].split(",")
            spec = args[1]
            return ("OK", [item for uid in uids for item in self._fetched(uid, spec)])
        raise AssertionError(f"unexpected command {command}")

    def _fetched(self, uid: str, spec: str) -> list[Any]:
        if uid not in self.messages:
            return []
        if "HEADER.FIELDS" in spec:
            raw = self.messages[uid]
            header = b"".join(
                line + b"\r\n"
                for line in raw.split(b"\n\n", 1)[0].split(b"\n")
                if line.lower().startswith(b"message-id:")
            )
            envelope = (
                f"{uid} (UID {uid} RFC822.SIZE {self.sizes[uid]} "
                f"BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(header) + 2}}}"
            ).encode()
            return [(envelope, header + b"\r\n"), b")"]
        body = self.messages[uid]
        return [(f"{uid} (UID {uid} BODY[] {{{len(body)}}}".encode(), body), b")"]


def _mailbox(fake: _FakeImap, *, folder: str = "Tickets") -> ImapMailbox:
    return ImapMailbox(cast(imaplib.IMAP4, fake), folder=folder, uid_validity=UID_VALIDITY)


def test_a_listing_takes_the_newest_and_names_every_message(monkeypatch: Any) -> None:
    """What comes back from a listing, and what never comes back.

    The newest are kept because UIDs ascend with arrival and the window can
    hold more mail than one pass should look at. The size cap is applied
    here, where the size is already known and nothing has been downloaded.
    And every message gets an id even when it has no Message-ID header: the
    id is what makes a re-poll idempotent, so there is no such thing as a
    message that does not need one.
    """
    monkeypatch.setattr(imap_mailbox, "MAX_MESSAGE_BYTES", 1000)
    fake = _FakeImap(
        {"101": _bare_email(), "102": _bare_email(), "103": _booking_email()},
        sizes={"101": 400, "102": 5000, "103": 400},
    )

    headers = _mailbox(fake).list_recent(since=date(2026, 9, 1), limit=2)

    # 101 fell outside the limit; 102 is over the size cap.
    assert [header.uid for header in headers] == ["103"]
    assert headers[0].message_id == "<booking-42@retailer.example>"
    assert ("SEARCH", "SINCE", "01-Sep-2026") in fake.commands
    # Only the two newest were even asked about.
    assert ("FETCH", "102,103", "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])") in (
        fake.commands
    )

    # A message with no Message-ID still gets one, stable for this mailbox.
    fake = _FakeImap({"77": _bare_email()})
    only = _mailbox(fake).list_recent(since=date(2026, 9, 1), limit=10)[0]
    assert only.message_id == f"<imap-{UID_VALIDITY}-77@imap.autotrain.invalid>"


def test_a_fetch_decodes_the_message_the_reader_needs() -> None:
    """The HTML part, the decoded subject, and the ticket with its path off.

    HTML over plain text is not cosmetic: a booking confirmation's plain
    alternative is routinely a stub telling you to open a browser, while the
    times and the price live only in the HTML — and journeys.intake reduces
    markup to words before the reader sees any of it either way.
    """
    fake = _FakeImap({"103": _booking_email()})

    message = _mailbox(fake).fetch("103")

    assert message is not None
    assert message.message_id == "<booking-42@retailer.example>"
    assert message.sender == "Retailer Bookings <noreply@retailer.example>"
    assert message.recipient == "traveller@example.com"
    assert message.subject == "Your booking — £24.50"
    assert "London Paddington 08:14" in message.body
    assert "View this email in a browser" not in message.body

    assert len(message.attachments) == 1
    ticket = message.attachments[0]
    assert ticket.filename == "e-ticket.pdf"
    assert ticket.content_type == "application/pdf"
    assert ticket.content == b"%PDF-1.4 ticket"

    # A message that has gone between the listing and now is not an error.
    assert _mailbox(fake).fetch("999") is None


def test_nothing_in_the_mailbox_is_ever_changed(monkeypatch: Any) -> None:
    """The promise the Mailbox protocol makes, kept twice over.

    The folder is opened read-only, so the server itself refuses a change,
    and every fetch uses BODY.PEEK, which does not set the Seen flag. This
    matters because the mailbox is a person's own: idempotency here comes
    from message ids already in the database, and a tool that instead marked
    mail read would be altering something it was only asked to look at — and
    would skip anything its owner had opened first.
    """
    fake = _FakeImap({"103": _booking_email()})
    monkeypatch.setattr(imap_mailbox.imaplib, "IMAP4_SSL", lambda host, port, timeout: fake)

    with ImapMailbox.connect(
        host="imap.example.com",
        username="traveller@example.com",
        password="app-password",
        folder="AutoTrain Tickets",
    ) as mailbox:
        mailbox.list_recent(since=date(2026, 9, 1), limit=10)
        mailbox.fetch("103")

    # A label with a space in it has to be quoted; imaplib quotes nothing.
    assert fake.selected == ('"AutoTrain Tickets"', True)
    fetches = [command for command in fake.commands if command[0] == "FETCH"]
    assert fetches, "expected at least one FETCH"
    assert all("BODY.PEEK" in command[2] for command in fetches)
    assert not any("STORE" in command[0] for command in fake.commands)
    assert fake.logged_out


def test_a_refused_search_is_an_error_not_an_empty_mailbox() -> None:
    """An empty answer and a failed one look alike and mean opposite things.

    Treated as empty, a folder that could not be opened would poll clean for
    ever and quietly stop importing anything. The raise reaches the
    scheduler's per-job isolation, which logs it and tries again next
    interval.
    """

    class _Refusing(_FakeImap):
        def uid(self, command: str, *args: str) -> Any:
            return ("NO", [None])

    with pytest.raises(imaplib.IMAP4.error):
        _mailbox(_Refusing({})).list_recent(since=date(2026, 9, 1), limit=10)
