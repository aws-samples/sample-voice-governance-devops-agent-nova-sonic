"""Structured JSON logging for the Voice_Service.

Every entry carries a timestamp and severity level; session-scoped loggers
bind the Voice_Session identifier into all entries produced while handling a
session (Req 19.4).

The line format (ISO-8601 UTC ``timestamp``, ``severity``, ``logger``,
``message``, extra fields) comes from :class:`shared.logging.JsonFormatter`.
This module adds the session scope: :func:`bind_session` stores the
Voice_Session identifier in a :class:`contextvars.ContextVar` — which
asyncio propagates into every task and callback started within the scope —
and :class:`SessionContextFilter` injects that identifier as a
``session_id`` attribute on every record emitted while the variable is set.
Records emitted outside any session scope carry no ``session_id`` field.

This module is imported as ``app.logging``; the plain ``import logging``
below resolves to the standard library because Python imports are absolute.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from shared.logging import JsonFormatter
from shared.logging import configure_logging as _configure_json_logging

__all__ = [
    "SessionContextFilter",
    "bind_session",
    "configure_logging",
    "current_session_id",
]

_session_id: ContextVar[str | None] = ContextVar("voice_session_id", default=None)


def current_session_id() -> str | None:
    """Return the Voice_Session identifier bound to the current context.

    Returns:
        The session identifier set by the innermost active
        :func:`bind_session` scope, or ``None`` when the caller is not
        handling a Voice_Session.
    """
    return _session_id.get()


@contextmanager
def bind_session(session_id: str) -> Iterator[None]:
    """Bind a Voice_Session identifier to the current execution context.

    While the scope is active, every log record emitted from the current
    context — including asyncio tasks and callbacks started within it —
    carries the identifier as its ``session_id`` field (Req 19.4). Scopes
    nest: an inner binding shadows an outer one and the outer binding is
    restored on exit. The previous binding is restored even when the body
    raises.

    Args:
        session_id: Identifier of the Voice_Session being handled.

    Yields:
        None. The binding is active for the duration of the ``with`` body.
    """
    token = _session_id.set(session_id)
    try:
        yield
    finally:
        _session_id.reset(token)


class SessionContextFilter(logging.Filter):
    """Injects the bound Voice_Session identifier into log records.

    Attached to the stdout handler by :func:`configure_logging`, so every
    record passing through it while a :func:`bind_session` scope is active
    gains a ``session_id`` attribute, which the JSON formatter renders as a
    top-level field (Req 19.4). Records emitted outside a session scope are
    passed through unchanged, and an explicit ``session_id`` supplied via
    ``extra=`` at the call site is left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the current session identifier onto a record, if bound.

        Args:
            record: The log record being processed.

        Returns:
            Always ``True``; the filter never drops records, it only
            annotates them.
        """
        session_id = _session_id.get()
        if session_id is not None and not hasattr(record, "session_id"):
            record.session_id = session_id
        return True


def configure_logging(level: int | str = "INFO") -> logging.Logger:
    """Configure structured JSON logging with Voice_Session scoping.

    Routes root logging to stdout as single-line JSON via
    :func:`shared.logging.configure_logging` and attaches a
    :class:`SessionContextFilter` to the handler, so every entry carries a
    timestamp and severity level and every entry produced while handling a
    Voice_Session carries the session identifier (Req 19.4). Calling it
    again reconfigures cleanly without duplicating handlers or filters.

    Args:
        level: Minimum severity the root logger lets through, as a
            ``logging`` level number or name. Defaults to ``"INFO"``.

    Returns:
        The configured root logger.
    """
    root = _configure_json_logging(level)
    session_filter = SessionContextFilter()
    for handler in root.handlers:
        if isinstance(handler.formatter, JsonFormatter) and not any(
            isinstance(existing, SessionContextFilter) for existing in handler.filters
        ):
            handler.addFilter(session_filter)
    return root
