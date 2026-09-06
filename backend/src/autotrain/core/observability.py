"""Logging that can be searched, and a request id that travels with every line.

ARCHITECTURE §1 (principle 6) and §7 promised structured logs from week one;
until now every process used logging.basicConfig's text lines. This module
is the whole of it, on the standard library:

* setup_logging(process, ...) — one handler on the root logger, JSON or
  text (Settings.log_format), with the process name on every line so four
  processes sharing one log stream stay tellable apart.
* request_id_var — a ContextVar the api's RequestIdMiddleware sets for the
  life of a request; the handler's filter copies it onto every record, so
  any line written while handling a request can be found again from the
  X-Request-ID the client was given.
* fields(stats) — a sweep's stats dataclass as `extra` fields, so
  logger.info("claim sweep complete", extra=fields(stats)) yields one
  searchable field per counter instead of a repr to parse.

No third-party logger: structlog or python-json-logger would add a
dependency for what is fifty lines here. If the shape outgrows this, the
seam is setup_logging and nothing else changes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# The attributes every LogRecord carries. Anything else on a record came in
# through `extra=` and is a field worth emitting.
_STANDARD_ATTRIBUTES = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "request_id",
    "process_name",
}


def fields(stats: Any) -> dict[str, Any]:
    """A dataclass's values as logging `extra`: one field per attribute."""
    return dataclasses.asdict(stats)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRIBUTES and not key.startswith("_")
    }


class _ContextFilter(logging.Filter):
    """Copies the ambient request id and the process name onto each record."""

    def __init__(self, process: str) -> None:
        super().__init__()
        self._process = process

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.process_name = self._process
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: what a log search indexes."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "process": getattr(record, "process_name", None),
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """For a terminal: level, logger, message, then any fields as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{record.levelname} {record.name}: {record.getMessage()}"
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            line += f" request_id={request_id}"
        for key, value in _extras(record).items():
            line += f" {key}={value}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_OURS = "autotrain_observability_handler"


def setup_logging(process: str, *, level: str, fmt: Literal["text", "json"]) -> None:
    """Install the one root handler. Called first thing by each entrypoint,
    in place of logging.basicConfig. Only a handler this function installed
    before is replaced: a test runner's own capture handler stays, so a
    test that runs an entrypoint's main() can still read its log lines."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(_ContextFilter(process))
    setattr(handler, _OURS, True)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _OURS, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
