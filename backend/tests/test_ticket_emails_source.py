"""The Claude ticket reader, with the SDK client played by a double: what it
sends (the cached instructions, the received date, the email, the schema)
and how it treats an answer that is not a reading. No network, no database.
The reading itself is judged in journeys.intake, tested through
test_intake_service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from autotrain.modules.journeys.service import ExtractedTicket
from autotrain.sources.ticket_emails import INSTRUCTIONS, ClaudeTicketExtractor, ExtractionFailed

TICKET = ExtractedTicket(
    is_ticket=True,
    retailer="LNER",
    kind="single",
    price_pence=2450,
    legs=[],
    confidence="low",
    notes="fixture answer",
)
RECEIVED = datetime(2026, 9, 6, 18, 30, tzinfo=UTC)


class _FakeClient:
    """Stands in for anthropic.Anthropic: records the one call the reader
    makes (client.messages.parse) and returns a prepared response."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


def _reader(response: Any) -> tuple[ClaudeTicketExtractor, _FakeClient]:
    fake = _FakeClient(response)
    return ClaudeTicketExtractor(api_key="test-key", client=cast(Any, fake)), fake


def test_sends_the_dated_email_under_cached_instructions_and_returns_the_parsed_ticket() -> None:
    reader, fake = _reader(SimpleNamespace(stop_reason="end_turn", parsed_output=TICKET))

    result = reader.extract(
        subject="Your e-ticket", body="Leeds to London Kings Cross", received_at=RECEIVED
    )

    assert result is TICKET
    [call] = fake.calls
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is ExtractedTicket
    # The instructions are the cached prefix: static text, marked once.
    assert call["system"] == [
        {"type": "text", "text": INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}
    ]
    # The one per-email fact the reader needs for a missing year goes in the
    # user turn, above the email, never in the cached instructions.
    assert call["messages"] == [
        {
            "role": "user",
            "content": (
                "Received: 2026-09-06\nSubject: Your e-ticket\n\nLeeds to London Kings Cross"
            ),
        }
    ]


def test_a_refusal_or_an_empty_answer_is_an_error_not_a_ticket() -> None:
    refused, _ = _reader(SimpleNamespace(stop_reason="refusal", parsed_output=None))
    with pytest.raises(ExtractionFailed, match="refused"):
        refused.extract(subject="s", body="b", received_at=RECEIVED)

    empty, _ = _reader(SimpleNamespace(stop_reason="max_tokens", parsed_output=None))
    with pytest.raises(ExtractionFailed, match="max_tokens"):
        empty.extract(subject="s", body="b", received_at=RECEIVED)
