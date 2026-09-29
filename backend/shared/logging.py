"""Structured JSON logging shared by the portal backend services.

Defines the single log line format emitted to stdout by both the
Voice_Service and the Notifier, where the container runtime and Lambda
forward it to CloudWatch Logs (Req 19.4). Every entry is one JSON object
carrying an ISO-8601 UTC ``timestamp``, a ``severity`` level, the
``logger`` name, and the rendered ``message``; any extra attributes
attached to the log record — via ``extra=`` at the call site or by an
injecting filter such as the Voice_Session filter in
``backend/voice_service/app/logging.py`` — are merged into the object as
additional top-level fields.

This module is imported as ``shared.logging``. Its name shadows the
standard library ``logging`` module only within dotted imports of the
``shared`` package; the plain ``import logging`` below resolves to the
standard library because Python imports are absolute.
"""

import json
import logging
import sys
from datetime import UTC, datetime

__all__ = [
    "JsonFormatter",
    "configure_logging",
]

_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON objects (Req 19.4).

    The rendered object always carries ``timestamp`` (ISO-8601 UTC, from
    the record's creation time), ``severity`` (the level name), ``logger``
    (the logger name), and ``message``. Record attributes beyond the
    standard ``logging.LogRecord`` set are merged in as additional fields,
    without ever overriding the four required keys. Values that are not
    JSON-serializable are rendered with ``str``, so a ``Secret`` wrapper
    logs as its redacting representation rather than its value.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record as one JSON line.

        Args:
            record: The log record to render.

        Returns:
            A single-line JSON object string containing ``timestamp``,
            ``severity``, ``logger``, and ``message``, plus any extra
            record attributes; formatted exception and stack information
            appear under ``exception`` and ``stack`` when present.
        """
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str)


def configure_logging(level: int | str = "INFO") -> logging.Logger:
    """Route root logging to stdout as structured JSON.

    Replaces any handlers on the root logger with a single stdout
    ``StreamHandler`` using :class:`JsonFormatter`, so every entry the
    process emits is one JSON line carrying a timestamp and severity
    level (Req 19.4). Calling it again reconfigures cleanly without
    duplicating handlers.

    Args:
        level: Minimum severity the root logger lets through, as a
            ``logging`` level number or name. Defaults to ``"INFO"``.

    Returns:
        The configured root logger.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    return root
