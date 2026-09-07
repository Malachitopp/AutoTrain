"""IMAP mailbox — the implementation of journeys.service.Mailbox.

Opens a real inbox over IMAPS and reads what is in it, instead of waiting
for a mail provider to post messages to a public endpoint. For a system that
serves one person this is strictly less machinery: no webhook, no shared
secret, no forwarding address, no DNS. It is also the only door that can
carry an attachment, and every operator's Delay Repay form wants a picture
of the ticket, so the e-ticket PDF arriving alongside the words is the whole
reason to prefer it.

READ-ONLY, and enforced twice. The folder is opened with EXAMINE
(readonly=True), which makes the server refuse a flag change, and every
fetch uses BODY.PEEK, which does not set the Seen flag. Nothing here marks
mail read, moves it or deletes it. That is a promise the Mailbox protocol
makes and this file keeps: the mailbox being polled is a person's own, and a
tool that quietly marked their mail read would be changing something it was
only asked to look at. Idempotency comes from message ids already in the
database, never from a flag.

Two round trips per poll, not one. Listing asks for headers and sizes only —
a few hundred bytes a message — and the caller drops everything it has
already stored before asking for a single body. A mailbox polled every
fifteen minutes over a fortnight's window would otherwise re-download the
same fortnight for ever.

Connections are per poll. An IMAP connection idle for fifteen minutes is
frequently dropped by the server without telling the client, so the
scheduler opens one, polls, and closes it, rather than holding one that
looks alive and is not.

Placement note: sources/ sits outside modules/ on purpose, exactly like
hsp.py and email.py. The journeys module defines the protocol and stays
transport-ignorant; the scheduler constructs this and hands it in.
"""

from __future__ import annotations

import email
import email.message
import email.policy
import imaplib
import logging
import re
from datetime import date
from types import TracebackType
from typing import Any

from autotrain.modules.journeys.service import MailboxAttachment, MailboxHeader, MailboxMessage

logger = logging.getLogger(__name__)

IMAPS_PORT = 993

# IMAP dates are English three-letter months whatever the machine's locale
# is, so they are built from this rather than from strftime.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# A message bigger than this is not a ticket. Listing knows each message's
# size before anything is downloaded, so the cap costs nothing and stops one
# 40 MB attachment holding a poll open for minutes on a home connection.
MAX_MESSAGE_BYTES = 25_000_000

# UIDs are asked for in groups, so one poll over a busy folder does not build
# a single command line thousands of numbers long.
_FETCH_CHUNK = 100

# The server echoes the UID back in every UID FETCH response (RFC 3501 makes
# it implicit), in an envelope line like: 7 (UID 4213 RFC822.SIZE 8192 ...
_UID_IN_RESPONSE = re.compile(rb"\bUID\s+(\d+)")
_SIZE_IN_RESPONSE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)")

# A LIST row is: (\HasNoChildren) "/" "[Gmail]/All Mail" — flags, the
# hierarchy delimiter, then the name, quoted. Only read when a folder failed
# to open, to say what would have worked instead.
_FOLDER_NAME = re.compile(r'"([^"]*)"\s*$')

# A FETCH record opens with the message's sequence number and a bracket:
# b'7 (UID 4213 ...'. Used to tell where one message's attributes end and the
# next begins, since imaplib hands them over as a flat list.
_RECORD_START = re.compile(rb"^\s*\d+\s+\(")

# Message-ID is optional in practice — plenty of mail arrives without one,
# and the database needs a unique key for every row. A synthetic id built
# from the folder's UIDVALIDITY and the message's UID is stable for as long
# as the mailbox is, which is exactly as long as the deduplication has to
# hold: UIDVALIDITY changing is the server saying "forget the old numbers",
# and re-importing then is the correct behaviour, not a bug.
_SYNTHETIC_DOMAIN = "imap.autotrain.invalid"


def _imap_date(day: date) -> str:
    """A date as IMAP's SEARCH wants it: 01-Sep-2026."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _clean(text: str) -> str:
    """Text from a message, fit to be stored and shown.

    Control characters go, and a NUL is the reason this exists rather than
    tidiness: Postgres text cannot hold one, psycopg refuses the parameter,
    and the whole message would fail to store. Because the poll's error
    handling then leaves the message unstored, it would be listed and fail
    again on every pass for the rest of the lookback window — a permanent
    loop started by any sender who puts a NUL in a filename or a subject
    (RFC 2231 percent-encoding and encoded words both allow it).

    Tabs and newlines are dropped with the rest: every value passing through
    here is a header or a filename, and both are single-line by definition.
    """
    return " ".join("".join(ch for ch in text if ch >= " " and ch != "\x7f").split())


def _quoted(folder: str) -> str:
    """A folder name safe to put in a command. Gmail labels routinely have
    spaces in them, and imaplib quotes nothing on the caller's behalf."""
    if folder.startswith('"') and folder.endswith('"'):
        return folder
    escaped = folder.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"')
    return '"' + escaped + '"'


class ImapMailbox:
    """One folder of one mailbox, open for reading.

    Built through `connect`, used inside a `with` block, and thrown away at
    the end of the poll. Not thread-safe and not meant to be: an IMAP
    connection is a single conversation, and the poll is one caller.
    """

    def __init__(self, connection: imaplib.IMAP4, *, folder: str, uid_validity: str) -> None:
        self._conn = connection
        self._folder = folder
        self._uid_validity = uid_validity

    @classmethod
    def connect(
        cls,
        *,
        host: str,
        username: str,
        password: str,
        folder: str = "INBOX",
        port: int = IMAPS_PORT,
        timeout_seconds: float = 30.0,
    ) -> ImapMailbox:
        """Open the folder for reading.

        IMAPS throughout: TLS from the first byte, on 993. There is no
        STARTTLS path and no plaintext port, because the password sent on
        the next line is a mailbox password.

        The timeout is the protocol's "bound your own time" requirement. A
        scheduler pass waits on this, and a mail server that accepts a
        connection and then says nothing would otherwise park the job until
        the process was restarted.
        """
        connection = imaplib.IMAP4_SSL(host, port, timeout=timeout_seconds)
        try:
            connection.login(username, password)
            # EXAMINE, not SELECT: the server itself refuses any change to
            # the folder for the life of this connection.
            status, detail = connection.select(_quoted(folder), readonly=True)
            if status != "OK":
                # imaplib does NOT raise here — it returns the refusal and
                # leaves the connection in AUTH state, so without this check
                # the failure surfaces at the next command as "SEARCH illegal
                # in state AUTH", which names neither the folder nor the
                # reason. A missing folder is the single most likely thing to
                # be wrong on a first run (a Gmail label only exists over IMAP
                # once it has been created and used), so it is worth saying
                # exactly that, with the alternatives.
                raise imaplib.IMAP4.error(_cannot_open(connection, folder, detail))
            uid_validity = _uid_validity_of(connection)
        except BaseException:
            # Including KeyboardInterrupt: a half-open socket to a mail
            # server is worth closing on the way out however we are leaving.
            cls._shutdown(connection)
            raise
        return cls(connection, folder=folder, uid_validity=uid_validity)

    def close(self) -> None:
        self._shutdown(self._conn)

    @staticmethod
    def _shutdown(connection: imaplib.IMAP4) -> None:
        """Log out, and never let the way we said goodbye become the error a
        caller sees — the work is already done by this point."""
        try:
            connection.logout()
        except Exception:
            logger.debug("imap logout failed; closing anyway", exc_info=True)

    def __enter__(self) -> ImapMailbox:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- The Mailbox protocol ---------------------------------------------

    def list_recent(self, *, since: date, limit: int) -> list[MailboxHeader]:
        """The newest `limit` messages received on or after `since`.

        SEARCH answers with every matching UID, which over a long window can
        be thousands; the newest are the ones that matter, and UIDs ascend
        with arrival, so the tail of the list is the right end to keep.

        Oversized messages are dropped here, where their size is already
        known, and said so in the log — a message skipped in silence is
        indistinguishable from one that never arrived.
        """
        status, data = self._conn.uid("SEARCH", "SINCE", _imap_date(since))
        if status != "OK" or not data or data[0] is None:
            raise imaplib.IMAP4.error(f"SEARCH failed on {self._folder!r}: {status}")
        uids = data[0].split()
        wanted = uids[-limit:] if limit > 0 else []
        if not wanted:
            return []

        headers: list[MailboxHeader] = []
        oversized = unnamed = unsized = 0
        for start in range(0, len(wanted), _FETCH_CHUNK):
            chunk = wanted[start : start + _FETCH_CHUNK]
            joined = b",".join(chunk).decode("ascii")
            status, response = self._conn.uid(
                "FETCH", joined, "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
            )
            if status != "OK":
                raise imaplib.IMAP4.error(f"FETCH of headers failed: {status}")
            for attributes, literal in _fetch_records(response):
                uid_match = _UID_IN_RESPONSE.search(attributes)
                if uid_match is None:
                    # No UID means nothing can be fetched later and no row
                    # could be deduplicated, so skipping is the only option —
                    # but it is COUNTED, because a poll that skips everything
                    # and a genuinely empty mailbox both report zero, and
                    # those two need to look different in a log.
                    unnamed += 1
                    continue
                size_match = _SIZE_IN_RESPONSE.search(attributes)
                if size_match is None:
                    # The cap cannot be applied to a message whose size the
                    # server did not give, and quietly downloading it anyway
                    # is the failure the cap exists to prevent.
                    unsized += 1
                    continue
                if int(size_match.group(1)) > MAX_MESSAGE_BYTES:
                    oversized += 1
                    continue
                uid = uid_match.group(1).decode("ascii")
                headers.append(MailboxHeader(uid=uid, message_id=self._message_id(literal, uid)))
        for count, why in (
            (oversized, f"over {MAX_MESSAGE_BYTES} bytes"),
            (unnamed, "the server sent no UID for them"),
            (unsized, "the server sent no size for them"),
        ):
            if count:
                logger.warning("mailbox %r: skipped %d message(s) — %s", self._folder, count, why)
        return headers

    def fetch(self, uid: str) -> MailboxMessage | None:
        """The whole message, decoded — or None if it is no longer there.

        A message listed a moment ago and gone now is ordinary: the mailbox
        belongs to a person, and people delete mail. The poll counts it and
        moves on.
        """
        status, response = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise imaplib.IMAP4.error(f"FETCH of message {uid} failed: {status}")
        records = _fetch_records(response)
        raw = records[0][1] if records else None
        if raw is None:
            return None
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        return MailboxMessage(
            uid=uid,
            message_id=_header(parsed, "Message-ID") or self._synthetic_id(uid),
            sender=_header(parsed, "From"),
            recipient=_header(parsed, "To"),
            subject=_header(parsed, "Subject"),
            body=_body_text(parsed),
            attachments=_attachments(parsed),
        )

    # --- Message ids ------------------------------------------------------

    def _message_id(self, header_literal: bytes | None, uid: str) -> str:
        """The Message-ID out of a header-only fetch, or a synthetic one.

        None is the server answering the body section as NIL or "" rather
        than as a literal, which is what a message with no Message-ID looks
        like on some servers. It takes a synthetic id like any other.
        """
        if not header_literal:
            return self._synthetic_id(uid)
        parsed = email.message_from_bytes(header_literal, policy=email.policy.default)
        return _header(parsed, "Message-ID") or self._synthetic_id(uid)

    def _synthetic_id(self, uid: str) -> str:
        return f"<imap-{self._uid_validity}-{uid}@{_SYNTHETIC_DOMAIN}>"


def _cannot_open(connection: imaplib.IMAP4, folder: str, detail: list[Any]) -> str:
    """Why the folder would not open, and what would have.

    The folder list is fetched only on this path, and only to be put in the
    message: the fix for "Unknown Mailbox: AutoTrain" is always one of the
    names the server would have accepted, and making someone go and ask for
    that list by hand is a diagnostic step we can just do for them. Gmail
    labels are case-sensitive here and nested ones are separated by '/', so
    the exact spelling is genuinely worth printing.
    """
    said = ""
    if detail and isinstance(detail[0], bytes):
        said = detail[0].decode("utf-8", "replace").strip()
    names = _selectable_folders(connection)
    known = ", ".join(repr(name) for name in names) if names else "none could be listed"
    return f"could not open folder {folder!r}: {said or 'refused'}. This account has: {known}"


def _selectable_folders(connection: imaplib.IMAP4) -> list[str]:
    """Every folder that can actually be opened, best effort.

    Never raises: this runs while another error is being reported, and
    failing to decorate a message must not replace it. \\Noselect entries
    are dropped because they cannot be the answer — Gmail's '[Gmail]' is one,
    and offering it as an option would send someone down a second dead end.
    """
    try:
        status, rows = connection.list()
    except Exception:
        logger.debug("could not list folders", exc_info=True)
        return []
    if status != "OK" or not rows:
        return []
    names: list[str] = []
    for row in rows:
        if not isinstance(row, bytes):
            continue
        line = row.decode("utf-8", "replace")
        if "\\Noselect" in line:
            continue
        match = _FOLDER_NAME.search(line)
        if match is not None:
            names.append(match.group(1))
    return names


def _uid_validity_of(connection: imaplib.IMAP4) -> str:
    """The selected folder's UIDVALIDITY, or '' if the server said nothing.

    Only ever used to build synthetic message ids, so an absent value costs
    nothing beyond a slightly less specific id.
    """
    try:
        value = connection.response("UIDVALIDITY")[1]
    except Exception:
        logger.debug("could not read UIDVALIDITY", exc_info=True)
        return ""
    if not value or not isinstance(value[0], bytes):
        return ""
    return value[0].decode("ascii", "replace")


def _fetch_records(response: list[Any]) -> list[tuple[bytes, bytes | None]]:
    """One (attributes, literal) record per message in a FETCH response.

    imaplib flattens a FETCH into a list that mixes tuples — a line and the
    literal it announced — with bare bytes for everything that had no
    literal: the closing bracket, and any attribute the server happened to
    put AFTER the literal. Reading only the tuples' first element is
    therefore reading part of a message's attributes and guessing at the
    rest, which matters because RFC 3501 fixes no order for them. A server
    answering

        * 1 FETCH (BODY[HEADER.FIELDS (MESSAGE-ID)] {21}
        <literal>
         UID 101 RFC822.SIZE 400)

    is entirely legal, and leaves the UID and the size in a bare item that a
    tuples-only reader never looks at — so every message would be skipped and
    the poll would report an empty mailbox for ever.

    So: accumulate everything belonging to one message, starting a new record
    at each item that opens one (a sequence number and a bracket). The
    literal is optional, because a body section is an nstring — a server may
    answer `""` or NIL for a message with no Message-ID rather than send an
    empty literal, and that message still needs a synthetic id rather than
    vanishing.
    """
    records: list[tuple[bytes, bytes | None]] = []
    attributes = b""
    literal: bytes | None = None
    started = False

    def flush() -> None:
        nonlocal attributes, literal
        if started:
            records.append((attributes, literal))
        attributes, literal = b"", None

    for item in response:
        head: bytes | None = None
        payload: bytes | None = None
        if isinstance(item, tuple) and len(item) >= 2:
            if isinstance(item[0], bytes):
                head = item[0]
            if isinstance(item[1], bytes):
                payload = item[1]
        elif isinstance(item, bytes):
            head = item
        if head is None and payload is None:
            continue
        if head is not None and _RECORD_START.match(head):
            flush()
            started = True
        if head is not None:
            attributes += b" " + head
        if payload is not None:
            literal = payload
    flush()
    return records


def _header(message: email.message.Message, name: str) -> str:
    """One header as a plain string, or ''.

    Encoded words are already decoded by the default policy. Malformed
    headers are common enough in real mail that a raise here would lose the
    whole message over a subject line, so anything the header parser cannot
    render becomes ''. Whitespace is folded out: these values are stored and
    shown in a browser.
    """
    try:
        value = message.get(name)
    except Exception:
        logger.debug("could not read header %s", name, exc_info=True)
        return ""
    if value is None:
        return ""
    try:
        return _clean(str(value))
    except Exception:
        logger.debug("could not render header %s", name, exc_info=True)
        return ""


def _body_text(message: email.message.Message) -> str:
    """The message's own words: its HTML part if it has one, else its plain
    text.

    HTML first on purpose, and not for looks. A booking confirmation's plain
    text alternative is routinely a stub telling you to view it in a browser,
    while the times, stations and price live only in the HTML — and the
    reader is given words either way, because journeys.intake reduces markup
    to text before anything sees it.
    """
    getter = getattr(message, "get_body", None)
    if getter is None:  # pragma: no cover — only a compat32 message
        return ""
    try:
        part = getter(preferencelist=("html", "plain"))
    except Exception:
        logger.debug("could not select a body part", exc_info=True)
        return ""
    if part is None:
        return ""
    return _part_text(part)


def _part_text(part: email.message.Message) -> str:
    """One part's text, tolerating a charset the sender got wrong.

    get_content() belongs to the modern EmailMessage that the default policy
    actually produces, but the parser is typed as returning the older base
    class — hence the lookup rather than a call. Its fallback is the same one
    a bad charset takes: the raw bytes, decoded permissively, because a
    mangled character in a booking confirmation is recoverable and a lost
    email is not.
    """
    getter = getattr(part, "get_content", None)
    if getter is not None:
        try:
            content = getter()
        except Exception:
            logger.debug("could not decode a body part; falling back to raw", exc_info=True)
        else:
            return content if isinstance(content, str) else ""
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return ""
    return raw.decode("utf-8", "replace")


def _attachments(message: email.message.Message) -> tuple[MailboxAttachment, ...]:
    """Every file the message carries, decoded, in the order it carries them.

    Nothing is filtered here — which types are worth keeping and how many is
    the journeys module's policy (intake.keepable_attachments), and this
    file is transport. Filenames are stripped of any directory part, because
    a sender chooses them and they end up in a row and on a screen.
    """
    iterator = getattr(message, "iter_attachments", None)
    if iterator is None:  # pragma: no cover — only a compat32 message
        return ()
    try:
        parts = list(iterator())
    except Exception:
        logger.debug("could not walk attachments", exc_info=True)
        return ()
    found: list[MailboxAttachment] = []
    for index, part in enumerate(parts, start=1):
        if _is_body_decoration(part):
            continue
        try:
            content = part.get_payload(decode=True)
        except Exception:
            logger.debug("could not decode an attachment", exc_info=True)
            continue
        if not isinstance(content, bytes) or not content:
            continue
        found.append(
            MailboxAttachment(
                filename=_safe_filename(part.get_filename(), index),
                content_type=part.get_content_type(),
                content=content,
            )
        )
    return tuple(found)


def _is_body_decoration(part: email.message.Message) -> bool:
    """Whether this part is part of the HTML, not a file the message carries.

    `iter_attachments` returns every non-body part, and that includes the
    logos and spacers a bulk mailer references from its own markup with
    cid: URLs. They are not attachments in any sense a person would mean,
    and left in they COMPETE with the ticket: the per-email cap keeps the
    first few files, so an email with six inline logos and one e-ticket
    stored six logos and no ticket. Measured, not theorised — it is exactly
    the shape a retailer's template produces.

    The test is deliberately narrow: inline AND carrying a Content-ID, which
    together mean "the markup points at this". A part merely marked inline
    without a Content-ID is kept, because some senders label a real
    attachment that way.
    """
    try:
        disposition = part.get_content_disposition()
        content_id = part.get("Content-ID")
    except Exception:
        logger.debug("could not read a part's disposition", exc_info=True)
        return False
    return disposition == "inline" and content_id is not None


def _safe_filename(name: str | None, index: int) -> str:
    """The file's name with any path taken off it, or a positional one.

    Both separators are cut whatever platform this runs on: the name came
    off the network, not off this filesystem, so a Windows path in a message
    read on Linux is exactly the case worth handling.
    """
    if not name:
        return f"attachment-{index}"
    base = _clean(name.replace(chr(92), "/").rsplit("/", 1)[-1])
    return base[:255] or f"attachment-{index}"
