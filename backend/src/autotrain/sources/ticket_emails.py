"""Claude ticket reader — the real implementation of
journeys.service.TicketExtractor (ARCHITECTURE §6a).

One structured-output call per email: the static instructions below go in
the system prompt with a cache marker (identical across every email, so
after the first call only the email itself is billed), the email goes in
the user turn, and the answer is validated against ExtractedTicket before it
comes back. Nothing here decides anything: whether the reading is good enough
to act on is journeys.intake's job, in plain code.

Placement note: sources/ sits outside modules/ on purpose, like hsp.py,
email.py and push.py. The journeys module defines the protocol and stays
vendor-ignorant; the scheduler constructs this and hands it in. When a
second call site arrives (§6a's claim-status emails), the client set-up here
moves to core/llm.py and both sources share it.
"""

from __future__ import annotations

import logging
from datetime import datetime

import anthropic

from autotrain.modules.journeys.service import ExtractedTicket

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"

# Extraction output is a small JSON object; this is a ceiling, not a target.
_MAX_TOKENS = 4096

# Deliberately static: every byte of this text is part of the cached prefix,
# so nothing that varies per email (dates, names) belongs here. The one
# per-email fact the reader needs — when the email arrived — goes in the
# user turn, above the email.
INSTRUCTIONS = """\
You read emails forwarded by UK rail passengers and fill in the schema you \
are given. The message starts with a "Received:" line giving the date the \
email reached us, then "Subject:", then the email itself. The email is a \
booking confirmation, e-ticket or itinerary only when it names specific \
trains the person is entitled to travel on. Marketing, refund notices, \
receipts for other things and messages about someone else's booking are not \
tickets: set is_ticket to false and leave the rest null or empty.

Read, never infer. Copy station codes, dates, times, prices and operator \
names exactly as the email states them. Station codes are the three-letter \
CRS codes (Manchester Piccadilly = MAN, London Euston = EUS). If the email \
prints a station name and you are certain of its CRS code, give the code; \
if not certain, give your best code and set confidence to "low". Times are \
UK local time as printed, 24-hour HH:MM. Dates are YYYY-MM-DD. If the ticket \
prints a date without a year, use the Received date to work it out: a \
booking is received before or shortly after travel, never long after.

The schema holds ONE ticket with ONE price. A return ticket with a fixed \
return train has two legs, in travel order. An open return, or a single, has \
one leg. A journey with a change of trains is one leg from the first \
departure to the final arrival. price_pence is the total the person paid for \
that ticket, in pence: £24.50 is 2450. If the email contains more than one \
ticket (for example two separate singles, each with its own price), do not \
merge them: set confidence to "low" and say so in notes.

operator_atoc_code only when certain: Avanti West Coast = VT, LNER = GR, \
Northern = NT, Great Western Railway = GW, Southeastern = SE, Southern = SN, \
Thameslink = TL, Great Northern = GN, South Western Railway = SW, \
CrossCountry = XC, TransPennine Express = TP, East Midlands Railway = EM, \
Greater Anglia = LE, ScotRail = SR, Transport for Wales = AW, Chiltern \
Railways = CH, West Midlands Trains (including West Midlands Railway and \
London Northwestern Railway) = WM, Merseyrail = ME, c2c = CC, Hull Trains = \
HT, Grand Central = GC, Lumo = LD, Heathrow Express = HX. Otherwise null. \
The seller is not the operator: a Trainline or LNER-branded email may cover \
a train run by someone else, so name the operator only if the email says who \
runs the train.

Set confidence to "low" whenever any value was worked out rather than read, \
and say what was unclear in notes. A null and a low confidence are always \
better than a guess: a wrong journey costs the passenger money.\
"""


class ExtractionFailed(Exception):
    """The model answered, but not with a ticket reading we can use."""


class ClaudeTicketExtractor:
    """TicketExtractor over the Anthropic Messages API.

    `client` is injectable for tests; production builds one from the API key.
    Timeouts and retries are the SDK's: a bounded wait per call (the intake
    sweep is waiting on it), two retries on transient failures, then the
    error propagates and the sweep counts one failed attempt for that email.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 90.0,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=2
        )
        self._model = model

    def extract(self, *, subject: str, body: str, received_at: datetime) -> ExtractedTicket:
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=[{"type": "text", "text": INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": f"Received: {received_at:%Y-%m-%d}\nSubject: {subject}\n\n{body}",
                }
            ],
            output_format=ExtractedTicket,
        )
        if response.stop_reason == "refusal":
            # The model declined to read this email. Not retryable by
            # re-sending the same text; the sweep records the failure and a
            # person looks at the row.
            raise ExtractionFailed("the reader refused this email")
        parsed = response.parsed_output
        if parsed is None:
            raise ExtractionFailed(f"no structured answer (stop_reason={response.stop_reason})")
        logger.debug(
            "ticket email read: is_ticket=%s legs=%d confidence=%s",
            parsed.is_ticket,
            len(parsed.legs),
            parsed.confidence,
        )
        return parsed
