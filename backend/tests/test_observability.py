"""Observability: the request id's round trip through the API, and the two
log formatters. setup_logging is deliberately not called here — it replaces
the root handler pytest's caplog relies on — so its pieces are tested
directly."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from autotrain.core import observability
from autotrain.core.observability import JsonFormatter, TextFormatter, fields, request_id_var


class TestRequestId:
    def test_every_response_carries_an_id_and_the_log_line_matches(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        fresh = client.get("/healthz")
        assert re.fullmatch(r"[0-9a-f]{32}", fresh.headers["x-request-id"])

        # A client's own id (a proxy, a retrying app) is kept, and the one
        # line written for the request carries it with the outcome.
        with caplog.at_level(logging.INFO, logger="autotrain.api.middleware"):
            kept = client.get("/operators", headers={"X-Request-ID": "proxy-abc.123"})
        assert kept.headers["x-request-id"] == "proxy-abc.123"
        [record] = [r for r in caplog.records if r.getMessage() == "request"]
        written = record.__dict__
        assert (written["method"], written["path"], written["status"]) == ("GET", "/operators", 200)
        assert written["duration_ms"] >= 0

        # An id that could corrupt a log line is replaced, never echoed.
        replaced = client.get("/healthz", headers={"X-Request-ID": "x" * 200})
        assert re.fullmatch(r"[0-9a-f]{32}", replaced.headers["x-request-id"])


@dataclass
class _Stats:
    examined: int = 3
    opened: int = 1


def test_formatters_carry_fields_and_the_request_id() -> None:
    logger = logging.getLogger("autotrain.test")
    record = logger.makeRecord(
        "autotrain.test",
        logging.INFO,
        __file__,
        1,
        "claim sweep complete",
        (),
        None,
        extra=fields(_Stats()),
    )
    token = request_id_var.set("req-1")
    try:
        observability._ContextFilter("scheduler").filter(record)
    finally:
        request_id_var.reset(token)

    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "claim sweep complete"
    assert (payload["level"], payload["logger"], payload["process"]) == (
        "INFO",
        "autotrain.test",
        "scheduler",
    )
    assert (payload["examined"], payload["opened"], payload["request_id"]) == (3, 1, "req-1")
    assert "ts" in payload

    assert TextFormatter().format(record) == (
        "INFO autotrain.test: claim sweep complete request_id=req-1 examined=3 opened=1"
    )
