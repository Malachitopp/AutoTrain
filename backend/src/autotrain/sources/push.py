"""Log push sender — the development implementation of
notifications.service.PushSender.

Delivers each push to the process log instead of a phone. The real
transports (FCM for Android, APNs for iOS) arrive with the React Native app
and its registered device tokens; until then this keeps the whole
notification pipeline — queue, locks, stamps, message building — exercisable
end to end, with deliveries you can actually read.

Placement note: sources/ sits outside modules/ on purpose, exactly like
hsp.py. The notifications module defines the protocol and stays
transport-ignorant; entrypoints construct a sender and hand it in.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LogPushSender:
    """PushSender that writes deliveries to the log. Never raises."""

    def send(self, *, token: str, platform: str, title: str, body: str) -> None:
        # The token is a credential-adjacent identifier: log a stub, not the
        # value, so a shipped log line can never be replayed against a real
        # push provider later.
        logger.info("push [%s] to %s…: %s — %s", platform, token[:8], title, body)
