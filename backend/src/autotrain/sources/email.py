"""Email senders — the implementations of identity.service.EmailSender.

Two of them, chosen by AUTOTRAIN_EMAIL_SENDER:

  * LogEmailSender writes the delivery to the process log. In development
    the log IS the inbox, so the whole login flow — token minting, hashing,
    storage, delivery, verification — stays exercisable end to end with a
    link you can actually click. _production_lockdown refuses to boot with
    this one selected.
  * ResendEmailSender posts the message to Resend's HTTPS API, which is the
    real thing.

Why Resend and not one of the dozen alternatives: its free tier sends 3,000
emails a month, 100 a day, with no card on file — comfortably the whole of
this product's login traffic for a long time — and it authenticates the
sending domain with SPF, DKIM and DMARC on that tier, which is what actually
decides whether a sign-in link lands in the inbox or the spam folder. The
transport is one HTTPS POST, so it works from anywhere outbound TCP 587 is
blocked, and it is swappable: everything provider-shaped lives in this one
class behind a Protocol the identity module owns.

Placement note: sources/ sits outside modules/ on purpose, exactly like
push.py and hsp.py. The identity module defines the protocol and stays
transport-ignorant; the api layer constructs a sender and hands it in.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from autotrain.modules.identity.service import EmailDeliveryError

logger = logging.getLogger(__name__)

RESEND_BASE_URL = "https://api.resend.com"

# A provider error is quoted back into an exception message that reaches a
# log. Bound it: a misrouted request can answer with a proxy's HTML error
# page, and a whole one of those in a log line helps nobody.
_ERROR_EXCERPT_CHARS = 300


class LogEmailSender:
    """EmailSender that writes deliveries to the log. Never raises."""

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        # Full body on purpose, login link included — in development the log
        # IS the inbox, so the link must be readable and clickable from it.
        # Unlike push.py's token[:8] redaction this is a single-use,
        # 15-minute login token in a dev-only transport, not a durable
        # credential worth hiding.
        logger.info("send email to %s: %s — %s", to, subject, body)


class ResendEmailSender:
    """EmailSender that delivers through Resend's HTTPS API.

    One instance per process, holding its HTTP client for the process
    lifetime — the same shape as HspSource, and for the same reason: a
    client built per request pays a fresh TLS handshake every time, while
    one that lives keeps the connection warm.

    Failure is never swallowed. Every path out of send_email that did not
    end in the provider accepting the message raises EmailDeliveryError,
    because request_login runs inside the caller's transaction: the raise
    rolls the login token's INSERT back with it, and no token can outlive
    the email that was supposed to carry it. There is deliberately no retry
    — a retry would hold a database transaction and a request thread open
    across a second network round trip, and the user pressing the button
    again is a better retry than one we perform on their behalf.
    """

    def __init__(self, client: httpx.Client, *, from_address: str) -> None:
        # Injected so tests drive the sender through httpx.MockTransport:
        # the client stack runs for real (headers, URL merging, JSON), only
        # the wire is faked. Production construction goes through
        # from_api_key.
        self._client = client
        self._from = from_address

    @classmethod
    def from_api_key(
        cls, api_key: str, *, from_address: str, timeout_seconds: float = 10.0
    ) -> ResendEmailSender:
        """The production constructor. The timeout is the contract's "bound
        your own delivery time": a request handler is blocked on this call,
        so an unresponsive provider must become an error in seconds rather
        than an API worker parked for ever."""
        return cls(
            httpx.Client(
                base_url=RESEND_BASE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout_seconds,
            ),
            from_address=from_address,
        )

    def close(self) -> None:
        self._client.close()

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        try:
            response = self._client.post(
                "/emails",
                json={"from": self._from, "to": [to], "subject": subject, "text": body},
            )
        except httpx.HTTPError as exc:
            # Timeout, DNS failure, refused connection, TLS problem: the
            # message may or may not have been sent, and we cannot know.
            # Treated as "did not send", which is the safe direction — the
            # worst case is a second link in the inbox, and the alternative
            # is a user staring at an empty one.
            raise EmailDeliveryError(f"could not reach the email provider: {exc}") from exc

        if response.is_error:
            raise EmailDeliveryError(_provider_refusal(response))

        # The recipient and the body stay out of the log on purpose: the
        # body carries a live login token, and the address is the personal
        # data this system works hardest not to spread. The provider's id
        # is the operational handle — it finds the delivery, the bounce and
        # the recipient in Resend's own dashboard when someone reports a
        # missing email.
        logger.info(
            "login email accepted by provider", extra={"provider_id": _message_id(response)}
        )


def _provider_refusal(response: httpx.Response) -> str:
    """What the provider said, as one line fit for an exception message.

    Resend answers a refusal with {"name": ..., "message": ...}; anything
    else — a gateway in front of it, a proxy, an outage page — is quoted
    raw and truncated. The status code is always included because it is the
    part that says what to do: 401 is a bad API key, 403 an unverified
    sending domain, 422 a malformed address, 429 the rate limit.
    """
    detail = ""
    try:
        payload: Any = response.json()
    except ValueError:
        detail = response.text.strip()
    else:
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("name") or payload)
        else:
            detail = str(payload)
    return f"email provider refused the message (HTTP {response.status_code}): " + (
        detail[:_ERROR_EXCERPT_CHARS] or "no detail given"
    )


def _message_id(response: httpx.Response) -> str:
    """The provider's id for an accepted message, or '' if it sent none.
    Never raises: a successful send must not become a failure because the
    acceptance body was shaped unexpectedly."""
    try:
        payload: Any = response.json()
    except ValueError:
        return ""
    return str(payload.get("id", "")) if isinstance(payload, dict) else ""
