# Feature: nova-sonic-support-portal, Property 19: Log entries carry required fields
"""Property test: structured log entries carry the required fields.

**Validates: Requirements 19.4**

For any log message, severity, and session context, the emitted structured
log entry contains a timestamp and severity level, and contains the
Voice_Session identifier exactly when the entry was produced while handling
a Voice_Session (Property 19).

The test mirrors the production wiring of ``app.logging.configure_logging``
— a handler carrying :class:`shared.logging.JsonFormatter` and
:class:`app.logging.SessionContextFilter` — on a private, per-example
``logging.Logger`` instance, so the global root logger is never mutated
between hypothesis examples. Messages are logged as literals with no
``args``, so ``%`` characters in generated text are never interpreted as
printf-style placeholders (``LogRecord.getMessage`` applies ``%`` formatting
only when args are present).
"""

import json
import logging
from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st
from shared.logging import JsonFormatter

from app.logging import SessionContextFilter, bind_session

_LEVELS: tuple[int, ...] = (
    logging.DEBUG,
    logging.INFO,
    logging.WARNING,
    logging.ERROR,
    logging.CRITICAL,
)


class _CapturingHandler(logging.Handler):
    """Collects each formatted log line instead of writing it anywhere."""

    def __init__(self) -> None:
        """Initialize the handler with an empty line collection."""
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and store the resulting JSON line.

        Args:
            record: The log record passing through the handler.
        """
        self.lines.append(self.format(record))


def _make_logger() -> tuple[logging.Logger, _CapturingHandler]:
    """Build a fresh, isolated logger wired like the production stack.

    Mirrors ``app.logging.configure_logging``: the handler carries the
    ``JsonFormatter`` and a ``SessionContextFilter``. The logger is a
    dedicated named logger reset on every call — handlers replaced,
    propagation disabled — so each example gets pristine state and the
    root logger configuration is never touched.

    Returns:
        The logger and its capturing handler.
    """
    handler = _CapturingHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SessionContextFilter())
    logger = logging.getLogger("tests.property.p19")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


@given(
    message=st.text(),
    level=st.sampled_from(_LEVELS),
    session_id=st.none() | st.text(min_size=1),
    extra=st.dictionaries(
        keys=st.from_regex(r"x_[a-z0-9]{1,8}", fullmatch=True),
        values=st.text(),
        max_size=3,
    ),
)
@settings(max_examples=100, deadline=None)
def test_log_entries_carry_required_fields(
    message: str,
    level: int,
    session_id: str | None,
    extra: dict[str, str],
) -> None:
    """Emitted entries carry timestamp, severity, and the session scope.

    The entry parses as JSON with an ISO-8601 timestamp and the severity
    level name; the message round-trips; ``session_id`` is present if and
    only if the entry was emitted inside a ``bind_session`` scope, and it
    equals the bound identifier; extra fields attached at the call site
    round-trip as top-level fields.

    Args:
        message: Arbitrary log message text, passed as a literal with no
            formatting args.
        level: Standard severity level to log at (DEBUG through CRITICAL).
        session_id: Voice_Session identifier to bind, or ``None`` to emit
            outside any session scope.
        extra: Additional fields attached at the call site via ``extra=``;
            keys use an ``x_`` prefix so they never collide with standard
            ``LogRecord`` attributes or the required entry fields.
    """
    logger, handler = _make_logger()

    if session_id is None:
        logger.log(level, message, extra=extra)
    else:
        with bind_session(session_id):
            logger.log(level, message, extra=extra)

    assert len(handler.lines) == 1
    entry = json.loads(handler.lines[0])

    timestamp = entry["timestamp"]
    assert isinstance(timestamp, str)
    assert datetime.fromisoformat(timestamp).tzinfo is not None

    assert entry["severity"] == logging.getLevelName(level)
    assert entry["message"] == message

    if session_id is None:
        assert "session_id" not in entry
    else:
        assert entry["session_id"] == session_id

    for key, value in extra.items():
        assert entry[key] == value
