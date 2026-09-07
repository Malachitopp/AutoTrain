"""Resend email sender tests. No network and no database: Resend itself is
played by httpx.MockTransport, so the client stack runs for real (the auth
header, URL merging, JSON encoding) and only the wire is faked — the same
harness as test_hsp_source.

What these pin is the contract identity.request_login depends on: a send
either happened or raised, never neither.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from autotrain.modules.identity.service import EmailDeliveryError
from autotrain.sources.email import RESEND_BASE_URL, ResendEmailSender

_FROM = "AutoTrain <login@in.autotrain.test>"
_TO = "rider@example.com"
_BODY = "Open this link to sign in to AutoTrain:\n\nhttps://app.test/login#token=s3cr3t\n"


def _sender(handler: Any) -> tuple[ResendEmailSender, list[httpx.Request]]:
    """A sender wired to a fake Resend, plus the list its requests land in."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(
        base_url=RESEND_BASE_URL,
        headers={"Authorization": "Bearer re_test_key"},
        transport=httpx.MockTransport(record),
    )
    return ResendEmailSender(client, from_address=_FROM), seen


def test_sends_the_request_the_provider_expects() -> None:
    """The wire format, pinned in one place. Everything here is something
    Resend requires and would refuse the message without: the POST target,
    the bearer key, the verified From, the recipient as a list, and the
    body under 'text' — 'body' or 'message' would be silently dropped and
    the user would receive an empty email with a working subject line."""
    sender, seen = _sender(lambda _r: httpx.Response(200, json={"id": "msg-1"}))

    sender.send_email(to=_TO, subject="Your AutoTrain sign-in link", body=_BODY)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{RESEND_BASE_URL}/emails"
    assert request.headers["Authorization"] == "Bearer re_test_key"
    import json

    assert json.loads(request.content) == {
        "from": _FROM,
        "to": [_TO],
        "subject": "Your AutoTrain sign-in link",
        "text": _BODY,
    }


@pytest.mark.parametrize(
    ("respond", "expected"),
    [
        # A refusal Resend shaped: the message it gives is the one that says
        # what to fix, so it has to survive into the exception.
        (
            lambda _r: httpx.Response(
                403,
                json={
                    "name": "validation_error",
                    "message": "The in.autotrain.test domain is not verified",
                },
            ),
            ["403", "not verified"],
        ),
        # A refusal something in front of Resend shaped — a gateway, a proxy,
        # an outage page. There is no 'message' to read, so the status and
        # the raw text are all there is, and neither may crash the parse.
        (lambda _r: httpx.Response(502, text="<html>Bad Gateway</html>"), ["502", "Bad Gateway"]),
        # An empty body: still an error, still has to name its status.
        (lambda _r: httpx.Response(429, text=""), ["429"]),
        # Never reached the provider at all. Indistinguishable from the
        # outside — the message may even have been sent — so it is treated
        # as a failure, which is the safe direction: a duplicate link in an
        # inbox beats an empty one.
        (
            lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("timed out", request=r)),
            ["could not reach"],
        ),
    ],
)
def test_every_failure_becomes_one_error_type(respond: Any, expected: list[str]) -> None:
    """One exception type out of every failure path, because request_login
    runs inside the caller's transaction: the raise is what rolls the login
    token's INSERT back, and a failure that escaped as some other type
    would leave a token in the database that no inbox can ever spend."""
    sender, _seen = _sender(respond)

    with pytest.raises(EmailDeliveryError) as excinfo:
        sender.send_email(to=_TO, subject="s", body=_BODY)

    message = str(excinfo.value)
    for fragment in expected:
        assert fragment in message, message


def test_the_login_link_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """A successful send logs that it happened and nothing more. The body
    carries a live login token — anyone who can read the log could sign in
    as that person — and the recipient is the personal data this system
    works hardest not to spread. The provider's id is the operational
    handle: it finds the delivery, and the recipient, in Resend's own
    dashboard when someone reports a missing email."""
    sender, _seen = _sender(lambda _r: httpx.Response(200, json={"id": "msg-42"}))

    with caplog.at_level(logging.DEBUG):
        sender.send_email(to=_TO, subject="Your AutoTrain sign-in link", body=_BODY)

    written = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "msg-42" in written
    assert "s3cr3t" not in written
    assert _TO not in written


def test_an_acceptance_without_an_id_still_counts_as_sent() -> None:
    """The id is for logging, not for judging. A 200 whose body is missing,
    empty or not JSON has still been accepted, and turning that into a
    failure would roll back a token whose email is genuinely on its way —
    the user would be told it failed while the link sat in their inbox."""
    for response in (httpx.Response(200, text=""), httpx.Response(202, json=[])):
        sender, _seen = _sender(lambda _r, resp=response: resp)
        sender.send_email(to=_TO, subject="s", body=_BODY)
