"""Log email sender — the development implementation of
identity.service.EmailSender.

Delivers each email to the process log instead of an inbox. A real provider
(SES or similar) arrives with deployment; until then this keeps the whole
login flow — token minting, hashing, storage, delivery, verification —
exercisable end to end: the log line is the inbox, and the link in it can be
clicked.

Placement note: sources/ sits outside modules/ on purpose, exactly like
push.py and hsp.py. The identity module defines the protocol and stays
transport-ignorant; the api layer constructs a sender and hands it in.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LogEmailSender:
    """EmailSender that writes deliveries to the log. Never raises."""

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        # Full body on purpose, login link included — in development the log
        # IS the inbox, so the link must be readable and clickable from it.
        # Unlike push.py's token[:8] redaction this is a single-use,
        # 15-minute login token in a dev-only transport, not a durable
        # credential worth hiding.
        logger.info("send email to %s: %s — %s", to, subject, body)
