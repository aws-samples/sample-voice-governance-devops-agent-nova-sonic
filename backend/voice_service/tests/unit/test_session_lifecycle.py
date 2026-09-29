"""Unit tests for the voice session manager's lifecycle edge cases.

Branch-specific companions to the session-manager property suites,
covering failure paths and boundary timings of
:class:`app.orchestration.voice_session_manager.VoiceSessionManager`
(Req 1.6, 2.2, 2.6, 7.6, 8.6):

- **Segmentation boundary** — the rollover fires at exactly 7 m 30 s of
  stream age: one second earlier nothing rolls over; at the threshold
  the session reports ``segmenting``, swaps onto a replacement stream
  opened with a context replay, and returns to ``live`` with an
  incremented segment count (Req 2.2).
- **Watchdog failure** — a rollover whose replacement stream never
  opens within the watchdog budget fails the session: the incrementally
  persisted partial transcript stays durable, the client receives the
  ``segmentation_failed`` error frame, the session persists as
  ``ERROR``, and the failure is logged with the Voice_Session
  identifier (Req 2.6).
- **Mid-session token expiry** — once the validated token's expiry
  passes, the session closes with the ``auth_expired`` error frame and
  audio received after the expiry instant is never forwarded to the
  stream (Req 7.6).
- **Stream-open failure** — a first Bedrock_Stream that cannot be
  opened ends the session with the ``bedrock_unavailable`` error frame
  before the close, leaving the session persisted in ``ERROR``
  (Req 1.6).
- **Resume rejection** — a ``resumeSessionId`` naming an unknown or
  already-terminal session is answered with the ``session_not_found``
  error frame and no session is created (Req 8.6).
- **Barge-in** — Nova Sonic's interruption signals (a ``textOutput``
  carrying the JSON interruption marker, and a ``contentEnd`` whose
  ``stopReason`` is ``INTERRUPTED``) each relay one ``interrupted``
  frame to the client, and the marker is never persisted or relayed as
  a transcript line.

Every test drives the manager end to end through the deterministic
in-memory fakes (:mod:`tests.fakes`), a scripted in-memory WebSocket
connection, and a fake-clock time driver whose sleepers wake only when
the test advances time — no AWS access and no real waiting beyond the
sub-second scripted watchdog budget.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Final

import pytest

from app.auth.jwt_validator import ValidatedToken
from app.domain.segmentation import (
    ROLLOVER_STREAM_AGE_SECONDS,
    WATCHDOG_BUDGET_SECONDS,
    ReplayEvent,
)
from app.domain.session import SessionState, VoiceSession
from app.domain.transcript import Role
from app.logging import SessionContextFilter
from app.orchestration.drain_manager import DrainManager
from app.orchestration.protection_manager import ProtectionManager
from app.orchestration.tool_router import ToolRouter
from app.orchestration.voice_session_manager import (
    AUTH_EXPIRED_MESSAGE,
    BEDROCK_UNAVAILABLE_MESSAGE,
    CLOSE_CODE_ERROR,
    CLOSE_CODE_NORMAL,
    DEFAULT_IDLE_GRACE_SECONDS,
    DEFAULT_IDLE_WARNING_SECONDS,
    IDLE_TIMEOUT_MESSAGE,
    IDLE_WARNING_MESSAGE,
    KEEPALIVE_IDLE_SECONDS,
    SEGMENTATION_FAILED_MESSAGE,
    SESSION_NOT_FOUND_MESSAGE,
    ConnectionClosed,
    VoiceSessionManager,
)
from app.ports.bedrock_stream import BedrockStreamPort
from tests.fakes import (
    FakeBedrockStream,
    FakeClock,
    FakeDevOpsAgent,
    FakeGuardrail,
    FakeSessionStore,
    FakeTaskProtection,
)

_SESSION_ID: Final = "id-1"
"""Deterministic session identifier: the harness id factory's first value."""

_ENGINEER_ID: Final = "engineer-1"
"""Cognito ``sub`` carried by the validated token of every session."""

_SYSTEM_PROMPT: Final = "You are a read-only diagnostic assistant."
"""System prompt the harness configures on the manager."""

_PARTIAL_LINE: Final = "The target group has two unhealthy targets."
"""Transcript line persisted before the failing rollover (Req 2.6)."""

_PRE_EXPIRY_AUDIO: Final = b"\x01\x02pre-expiry-pcm"
"""Client audio frame arriving before the token expiry (forwarded)."""

_POST_EXPIRY_AUDIO: Final = b"\x03\x04post-expiry-pcm"
"""Client audio frame arriving after the token expiry (dropped, Req 7.6)."""

_LONG_TOKEN_LIFETIME_SECONDS: Final = 3600.0
"""Default token lifetime, far beyond every advance a test performs."""

_SHORT_TOKEN_LIFETIME_SECONDS: Final = 60.0
"""Token lifetime of the mid-session expiry test (Req 7.6)."""

_TINY_WATCHDOG_SECONDS: Final = 0.05
"""Sub-second watchdog budget so the overrun test finishes promptly."""

_TEST_DEADLINE_SECONDS: Final = 5.0
"""Real-time safety bound on awaiting the session task's completion."""

_WAIT_YIELDS: Final = 2000
"""Event-loop passes granted to a condition before the test fails."""

_ADVANCE_YIELDS: Final = 50
"""Event-loop passes granted to woken sleepers after a time advance."""

_MANAGER_TIMER_COUNT: Final = 4
"""Sleepers the manager arms per live session: segmentation timer,
token-expiry watchdog, the input keepalive, and the idle watchdog (the
harness keeps collaborator timers off the driver, so this count is
exact)."""

_RECEIVES_AFTER_BOTH_AUDIO_FRAMES: Final = 4
"""``receive`` entries proving both audio frames were fully handled:
the opening ``session.start`` (1), each audio frame (2, 3), and the
pump's re-entry after handling the second frame (4)."""

_SEGMENT_COUNT_AFTER_ONE_ROLLOVER: Final = 2
"""Expected ``segment_count`` after one completed rollover: the first
stream plus its replacement."""

_SEGMENTATION_FAILED_EVENT: Final = "session.segmentation_failed"
"""``event`` extra field of the rollover-failure log entry (Req 2.6)."""

_AUTH_EXPIRED_EVENT: Final = "session.auth_expired"
"""``event`` extra field of the token-expiry log entry (Req 7.6)."""

_STREAM_OPEN_FAILED_EVENT: Final = "session.stream_open_failed"
"""``event`` extra field of the stream-open-failure log entry (Req 1.6)."""

_RESUME_REJECTED_EVENT: Final = "session.resume_rejected"
"""``event`` extra field of the rejected-resume log entry (Req 8.6)."""

_INTERRUPTED_EVENT: Final = "session.interrupted"
"""``event`` extra field of the barge-in log entry."""

_INTERRUPTION_MARKER: Final = '{ "interrupted" : true }'
"""Exact ``textOutput`` content Nova Sonic emits on engineer barge-in."""

_PROTOCOL_VIOLATION_EVENT: Final = "session.protocol_violation"
"""``event`` extra field of the malformed-frame log entry."""

_TYPED_REQUEST: Final = "describe instance i-0abc123"
"""Typed engineer request used by the ``text.input`` tests."""

_IDLE_WARNING_EVENT: Final = "session.idle_warning"
"""``event`` field of the idle-warning log record."""

_IDLE_TIMEOUT_EVENT: Final = "session.idle_timeout"
"""``event`` field of the idle-timeout log record."""

_ENGINEER_TURN: Final = "still here, keep going"
"""ASR text of the engineer turn that clears the idle interval."""

_KEEPALIVE_EVENT: Final = "session.keepalive"
"""``event`` extra field of the keepalive log entry."""


class _RecordingHandler(logging.Handler):
    """Collects raw log records instead of writing them anywhere."""

    def __init__(self) -> None:
        """Initialize the handler with an empty record collection."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record for later assertions.

        Args:
            record: The log record passing through the handler.
        """
        self.records.append(record)


def _make_logger() -> tuple[logging.Logger, _RecordingHandler]:
    """Build a fresh, isolated logger that captures raw records.

    The logger is a dedicated named logger reset on every call — handlers
    replaced, propagation disabled — so each test observes only its own
    records and the root logger configuration is never touched. The
    handler carries a :class:`SessionContextFilter`, so records emitted
    inside the manager's ``bind_session`` scope carry the session
    identifier exactly as they would in production (Req 19.4).

    Returns:
        The logger and its recording handler.
    """
    handler = _RecordingHandler()
    handler.addFilter(SessionContextFilter())
    logger = logging.getLogger("tests.unit.session_lifecycle")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


class _IdFactory:
    """Deterministic identifier factory: ``id-1``, ``id-2``, ..."""

    def __init__(self) -> None:
        """Initialize the factory at zero issued identifiers."""
        self._count = 0

    def __call__(self) -> str:
        """Return the next identifier in sequence.

        Returns:
            ``id-1`` for the first call, ``id-2`` for the second, and so
            on; the manager consumes the first value as the session id.
        """
        self._count += 1
        return f"id-{self._count}"


class _TimeDriver:
    """Deterministic replacement for ``asyncio.sleep`` on a fake clock.

    Sleepers register an absolute wake deadline computed from the
    injected :class:`FakeClock` and suspend on an event;
    :meth:`advance` moves the clock and wakes every sleeper whose
    deadline has passed, so timer-driven code (the segmentation timer,
    the token-expiry watchdog) runs without any real waiting.
    """

    def __init__(self, clock: FakeClock) -> None:
        """Bind the driver to the clock its deadlines are computed from.

        Args:
            clock: The fake clock shared with the code under test.
        """
        self._clock = clock
        self._waiters: list[tuple[float, asyncio.Event]] = []

    @property
    def pending(self) -> int:
        """Return how many sleepers are currently suspended.

        Returns:
            The number of registered, not-yet-woken sleep calls.
        """
        return len(self._waiters)

    async def sleep(self, delay: float) -> None:
        """Suspend until the clock is advanced past ``now + delay``.

        Args:
            delay: Requested delay in seconds; non-positive delays just
                yield control once and return.
        """
        if delay <= 0:
            await asyncio.sleep(0)
            return
        wake_at = self._clock.now() + delay
        event = asyncio.Event()
        self._waiters.append((wake_at, event))
        await event.wait()

    async def advance(self, seconds: float) -> None:
        """Move the clock forward and wake every sleeper now due.

        Args:
            seconds: Amount of time to add to the clock; ``0.0`` wakes
                sleepers whose deadline the clock has already passed.
        """
        self._clock.advance(seconds)
        now = self._clock.now()
        due = [event for wake_at, event in self._waiters if wake_at <= now]
        self._waiters = [
            (wake_at, event) for wake_at, event in self._waiters if wake_at > now
        ]
        for event in due:
            event.set()
        for _ in range(_ADVANCE_YIELDS):
            await asyncio.sleep(0)


async def _sleep_forever(_delay: float) -> None:
    """Suspend until cancelled, keeping collaborator timers inert.

    Injected into the protection and drain managers so their internal
    timers never register with the test's time driver — the driver's
    :attr:`_TimeDriver.pending` count then reflects only the session
    manager's own timers.

    Args:
        _delay: Ignored requested delay.
    """
    await asyncio.Event().wait()


class _ScriptedConnection:
    """In-memory ``VoiceConnection``: scripted inbound, recorded outbound.

    Frames the test feeds are delivered to ``receive`` in feed order;
    everything the manager sends is recorded for assertions. After
    :meth:`close` has run, pending and subsequent ``receive`` calls
    raise :class:`ConnectionClosed`, matching the protocol contract.

    Attributes:
        sent_texts: Every text frame sent by the manager, in order.
        sent_bytes: Every binary frame sent by the manager, in order.
        closes: Every ``(code, reason)`` close call, in order.
        receive_calls: Number of ``receive`` entries so far; entry N+1
            proves the handling of frame N completed.
    """

    def __init__(self) -> None:
        """Initialize an open connection with an empty inbound script."""
        self.sent_texts: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closes: list[tuple[int, str]] = []
        self.receive_calls = 0
        self._inbound: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._closed = False

    def feed(self, frame: str | bytes) -> None:
        """Script one inbound frame for ``receive`` to deliver.

        Args:
            frame: Text control frame or binary PCM payload.
        """
        self._inbound.put_nowait(frame)

    def disconnect(self) -> None:
        """Script a client disconnect: the next ``receive`` raises."""
        self._inbound.put_nowait(None)

    async def send_text(self, data: str) -> None:
        """Record one text frame sent to the client.

        Args:
            data: Serialized JSON control frame.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        if self._closed:
            raise ConnectionClosed("send-after-close")
        self.sent_texts.append(data)

    async def send_bytes(self, data: bytes) -> None:
        """Record one binary frame sent to the client.

        Args:
            data: Raw PCM audio bytes.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        if self._closed:
            raise ConnectionClosed("send-after-close")
        self.sent_bytes.append(data)

    async def receive(self) -> str | bytes:
        """Deliver the next scripted frame.

        Returns:
            The next fed frame, in feed order.

        Raises:
            ConnectionClosed: When the connection was closed locally or
                a scripted disconnect is reached.
        """
        self.receive_calls += 1
        if self._closed:
            raise ConnectionClosed("receive-after-close")
        item = await self._inbound.get()
        if item is None:
            self._inbound.put_nowait(None)
            raise ConnectionClosed("client-disconnected")
        return item

    async def close(self, code: int, reason: str) -> None:
        """Record the close and unblock any pending ``receive``.

        Args:
            code: WebSocket close code.
            reason: Short close reason token.
        """
        self.closes.append((code, reason))
        self._closed = True
        self._inbound.put_nowait(None)


class _StreamFactory:
    """Hands out scripted stream fakes, one per open, recording creations.

    Attributes:
        created: The streams handed out so far, in creation order.
    """

    def __init__(self, streams: Sequence[BedrockStreamPort]) -> None:
        """Initialize the factory with its scripted streams.

        Args:
            streams: The stream instances to hand out, in order; one per
                expected ``stream_factory`` call.
        """
        self._streams = list(streams)
        self.created: list[BedrockStreamPort] = []

    def __call__(self) -> BedrockStreamPort:
        """Return the next scripted stream.

        Returns:
            The stream at the position of this call.

        Raises:
            IndexError: If the manager opens more streams than the test
                scripted — a test-design failure surfaced loudly.
        """
        stream = self._streams[len(self.created)]
        self.created.append(stream)
        return stream


class _HangingOpenStream(FakeBedrockStream):
    """Fake stream whose ``open`` suspends forever (cancellable).

    Drives the segmentation watchdog overrun (Req 2.6): the replacement
    stream never finishes opening, so ``asyncio.timeout`` around the
    rollover expires and cancels the pending open.
    """

    async def open(self, events: Sequence[ReplayEvent]) -> None:
        """Suspend on an event that is never set.

        Args:
            events: Replay sequence the rollover attempted to deliver;
                ignored, since the open never completes.
        """
        await asyncio.Event().wait()


@dataclass(frozen=True, slots=True)
class _Harness:
    """Assembled session manager under test with its fakes and captures.

    Attributes:
        manager: The ``VoiceSessionManager`` wired to the fakes below.
        connection: Scripted in-memory connection served by the manager.
        store: In-memory Session_Store recording persists.
        clock: Injected fake clock (wall and monotonic time alike).
        driver: Time driver waking the manager's timers on demand.
        factory: Stream factory recording every opened stream.
        token: Validated token that authenticated the connection.
        handler: Recording handler capturing every manager log record.
    """

    manager: VoiceSessionManager
    connection: _ScriptedConnection
    store: FakeSessionStore
    clock: FakeClock
    driver: _TimeDriver
    factory: _StreamFactory
    token: ValidatedToken
    handler: _RecordingHandler


def _make_harness(
    *,
    streams: Sequence[BedrockStreamPort] | None = None,
    token_lifetime_seconds: float = _LONG_TOKEN_LIFETIME_SECONDS,
    watchdog_seconds: float = WATCHDOG_BUDGET_SECONDS,
) -> _Harness:
    """Assemble a session manager over fresh fakes.

    The manager's wall clock, monotonic clock, and sleep are all bound
    to one :class:`FakeClock` through the :class:`_TimeDriver`, so its
    segmentation timer and token watchdog fire only when a test
    advances time. The protection and drain managers get an inert sleep
    so they never touch the driver.

    Args:
        streams: Streams the factory hands out, in order; two fresh
            ``FakeBedrockStream`` instances when omitted.
        token_lifetime_seconds: Token validity from the clock's start;
            defaults far beyond every advance a test performs.
        watchdog_seconds: Segmentation watchdog budget (real seconds,
            enforced by ``asyncio.timeout``); the design default unless
            a test shrinks it.

    Returns:
        The assembled harness: manager, fakes, clock driver, and log
        capture.
    """
    clock = FakeClock()
    driver = _TimeDriver(clock)
    store = FakeSessionStore()
    logger, handler = _make_logger()
    factory = _StreamFactory(
        streams if streams is not None else (FakeBedrockStream(), FakeBedrockStream())
    )
    protection = ProtectionManager(
        FakeTaskProtection(clock), sleep=_sleep_forever, logger=logger
    )
    drain = DrainManager(sleep=_sleep_forever, logger=logger)
    tool_router = ToolRouter(
        FakeGuardrail(),
        FakeDevOpsAgent(),
        store,
        clock=clock.now,
        logger=logger,
    )
    token = ValidatedToken(
        sub=_ENGINEER_ID,
        username=_ENGINEER_ID,
        expires_at=clock.now() + token_lifetime_seconds,
        claims={},
    )
    manager = VoiceSessionManager(
        stream_factory=factory,
        store=store,
        tool_router=tool_router,
        protection=protection,
        drain=drain,
        system_prompt=_SYSTEM_PROMPT,
        watchdog_seconds=watchdog_seconds,
        clock=clock.now,
        monotonic=clock.now,
        sleep=driver.sleep,
        logger=logger,
        id_factory=_IdFactory(),
    )
    return _Harness(
        manager=manager,
        connection=_ScriptedConnection(),
        store=store,
        clock=clock,
        driver=driver,
        factory=factory,
        token=token,
        handler=handler,
    )


@asynccontextmanager
async def _running_session(harness: _Harness) -> AsyncIterator[asyncio.Task[None]]:
    """Run the harness's session as a task, guaranteeing cleanup.

    Args:
        harness: The assembled harness whose manager serves the
            scripted connection.

    Yields:
        The running ``run_session`` task, for the test to await once
        the session should have ended.
    """
    task = asyncio.create_task(
        harness.manager.run_session(harness.connection, harness.token),
        name="session-under-test",
    )
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        elif not task.cancelled():
            task.exception()


async def _finished(task: asyncio.Task[None]) -> None:
    """Await the session task's completion within the test deadline.

    Args:
        task: The running ``run_session`` task.

    Raises:
        TimeoutError: If the session does not finish within the
            real-time safety deadline.
    """
    async with asyncio.timeout(_TEST_DEADLINE_SECONDS):
        await task


async def _wait_for(predicate: Callable[[], bool], description: str) -> None:
    """Yield to the event loop until a condition holds.

    Args:
        predicate: Zero-argument condition polled between event-loop
            passes; nothing here waits on real time.
        description: Failure description reported when the yield budget
            is exhausted.
    """
    for _ in range(_WAIT_YIELDS):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail(f"Condition never became true: {description}")


async def _go_live(harness: _Harness) -> None:
    """Open a fresh session and wait until it reports ``live``.

    Args:
        harness: The harness whose connection receives the opening
            ``session.start`` frame.
    """
    harness.connection.feed(_session_start_frame())
    await _wait_for(
        lambda: _states(harness.connection) == ["connecting", "live"],
        "session reported live",
    )


def _session_start_frame(resume_session_id: str | None = None) -> str:
    """Build a ``session.start`` client frame.

    Args:
        resume_session_id: Session to resume, or ``None`` for a fresh
            session.

    Returns:
        The frame's wire JSON text.
    """
    payload: dict[str, object] = {"type": "session.start"}
    if resume_session_id is not None:
        payload["resumeSessionId"] = resume_session_id
    return json.dumps(payload)


def _session_end_frame() -> str:
    """Build a ``session.end`` client frame.

    Returns:
        The frame's wire JSON text.
    """
    return json.dumps({"type": "session.end"})


def _text_output_event(text: str) -> dict[str, object]:
    """Build one ``textOutput`` stream event carrying a response line.

    Args:
        text: The response text of the event.

    Returns:
        The decoded event mapping, ready for ``FakeBedrockStream.feed``.
    """
    return {"type": "textOutput", "role": "ASSISTANT", "content": text}


def _frames(connection: _ScriptedConnection) -> list[dict[str, object]]:
    """Parse every text frame the manager sent.

    Args:
        connection: The scripted connection whose sends to parse.

    Returns:
        The decoded JSON payloads, in send order.

    Raises:
        AssertionError: If a sent frame is not a JSON object.
    """
    parsed: list[dict[str, object]] = []
    for text in connection.sent_texts:
        loaded: object = json.loads(text)
        assert isinstance(loaded, dict)
        parsed.append(loaded)
    return parsed


def _states(connection: _ScriptedConnection) -> list[object]:
    """Extract the ``state`` values of the sent ``session.state`` frames.

    Args:
        connection: The scripted connection whose sends to inspect.

    Returns:
        The lowercase state values, in send order (Req 9.4).
    """
    return [
        frame.get("state")
        for frame in _frames(connection)
        if frame.get("type") == "session.state"
    ]


def _interrupted_frame_count(connection: _ScriptedConnection) -> int:
    """Count the ``interrupted`` frames the manager sent.

    Args:
        connection: The scripted connection whose sends to inspect.

    Returns:
        How many barge-in notices were relayed to the client.
    """
    return sum(
        1 for frame in _frames(connection) if frame.get("type") == "interrupted"
    )


def _error_frames(connection: _ScriptedConnection) -> list[tuple[object, object]]:
    """Extract the ``(category, message)`` pairs of the sent error frames.

    Args:
        connection: The scripted connection whose sends to inspect.

    Returns:
        The pairs in send order; empty when no error frame was sent.
    """
    return [
        (frame.get("category"), frame.get("message"))
        for frame in _frames(connection)
        if frame.get("type") == "error"
    ]


def _idle_warning_recoverable_flags(
    connection: _ScriptedConnection,
) -> list[object]:
    """Extract the ``recoverable`` flags of the sent ``idle_warning`` frames.

    The flag is what tells the frontend the session survives this notice,
    so it is asserted rather than assumed.

    Args:
        connection: The scripted connection whose sends to inspect.

    Returns:
        The flags in send order; empty when no idle warning was sent.
    """
    return [
        frame.get("recoverable")
        for frame in _frames(connection)
        if frame.get("category") == "idle_warning"
    ]


def _records_for(handler: _RecordingHandler, event: str) -> list[logging.LogRecord]:
    """Filter captured records down to one manager log event.

    Args:
        handler: The harness's recording handler.
        event: Expected ``event`` extra field value.

    Returns:
        The matching records, in emission order.
    """
    return [
        record
        for record in handler.records
        if getattr(record, "event", None) == event
    ]


async def test_segmentation_boundary_fires_at_rollover_age() -> None:
    """The rollover triggers at exactly 7 m 30 s of stream age (Req 2.2).

    One second before the threshold nothing rolls over; at exactly
    450 s of stream age the session reports ``segmenting``, closes the
    expiring stream, opens a replacement with a context replay followed
    by a fresh interactive audio block, and returns to ``live`` with an
    incremented segment count. A graceful end then closes normally.
    """
    first = FakeBedrockStream()
    second = FakeBedrockStream()
    harness = _make_harness(streams=(first, second))

    async with _running_session(harness) as task:
        await _go_live(harness)
        await _wait_for(
            lambda: harness.driver.pending == _MANAGER_TIMER_COUNT,
            "manager timers armed",
        )

        await harness.driver.advance(ROLLOVER_STREAM_AGE_SECONDS - 1.0)
        assert len(harness.factory.created) == 1
        assert "segmenting" not in _states(harness.connection)

        await harness.driver.advance(1.0)
        await _wait_for(
            lambda: _states(harness.connection)
            == ["connecting", "live", "segmenting", "live"],
            "rollover completed",
        )

        harness.connection.feed(_session_end_frame())
        await _finished(task)

    assert first.closed
    assert len(second.opened_events) == 1
    assert second.opened_events[0][0]["type"] == "sessionStart"
    assert second.sent_events[0]["type"] == "contentStart"
    assert _states(harness.connection) == [
        "connecting",
        "live",
        "segmenting",
        "live",
        "ended",
    ]
    assert _error_frames(harness.connection) == []
    assert harness.connection.closes == [(CLOSE_CODE_NORMAL, "client_end")]
    session = harness.store.sessions[_SESSION_ID]
    assert session.state is SessionState.ENDED
    assert session.segment_count == _SEGMENT_COUNT_AFTER_ONE_ROLLOVER


async def test_watchdog_overrun_fails_rollover_and_keeps_transcript() -> None:
    """A rollover overrunning its watchdog fails the session (Req 2.6).

    The replacement stream's open hangs past the (shrunk) watchdog
    budget: the already-persisted partial transcript stays durable, the
    client receives the ``segmentation_failed`` error frame after the
    ``error`` state report, the session persists as ``ERROR``, and the
    failure is logged with the Voice_Session identifier and the
    ``TimeoutError`` that expired the watchdog.
    """
    first = FakeBedrockStream()
    second = _HangingOpenStream()
    harness = _make_harness(
        streams=(first, second), watchdog_seconds=_TINY_WATCHDOG_SECONDS
    )

    async with _running_session(harness) as task:
        await _go_live(harness)
        await _wait_for(
            lambda: harness.driver.pending == _MANAGER_TIMER_COUNT,
            "manager timers armed",
        )

        first.feed(_text_output_event(_PARTIAL_LINE))
        await _wait_for(
            lambda: len(harness.store.transcripts.get(_SESSION_ID, [])) == 1,
            "transcript entry persisted",
        )

        await harness.driver.advance(ROLLOVER_STREAM_AGE_SECONDS)
        await _finished(task)

    entries = harness.store.transcripts[_SESSION_ID]
    assert [entry.text for entry in entries] == [_PARTIAL_LINE]
    assert _states(harness.connection) == [
        "connecting",
        "live",
        "segmenting",
        "error",
    ]
    assert _error_frames(harness.connection) == [
        ("segmentation_failed", SEGMENTATION_FAILED_MESSAGE)
    ]
    assert harness.connection.closes == [(CLOSE_CODE_ERROR, "segmentation_failed")]
    assert harness.store.sessions[_SESSION_ID].state is SessionState.ERROR
    records = _records_for(harness.handler, _SEGMENTATION_FAILED_EVENT)
    assert len(records) == 1
    assert getattr(records[0], "session_id", None) == _SESSION_ID
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], TimeoutError)


async def test_token_expiry_closes_session_and_drops_late_audio() -> None:
    """Mid-session token expiry ends the session (Req 7.6).

    Audio arriving before the expiry reaches the stream; audio arriving
    after the expiry instant is dropped without being forwarded. Once
    the watchdog observes the expiry, the session persists as ``ENDED``
    and the client receives the ``auth_expired`` error frame before the
    failure-code close.
    """
    first = FakeBedrockStream()
    harness = _make_harness(
        streams=(first,),
        token_lifetime_seconds=_SHORT_TOKEN_LIFETIME_SECONDS,
    )

    async with _running_session(harness) as task:
        await _go_live(harness)
        await _wait_for(
            lambda: harness.driver.pending == _MANAGER_TIMER_COUNT,
            "manager timers armed",
        )

        harness.connection.feed(_PRE_EXPIRY_AUDIO)
        await _wait_for(
            lambda: len(first.sent_audio) == 1, "pre-expiry audio forwarded"
        )

        # Move the wall clock past the expiry without waking the
        # watchdog, then deliver audio that must be dropped (Req 7.6).
        harness.clock.advance(_SHORT_TOKEN_LIFETIME_SECONDS + 1.0)
        harness.connection.feed(_POST_EXPIRY_AUDIO)
        await _wait_for(
            lambda: harness.connection.receive_calls
            >= _RECEIVES_AFTER_BOTH_AUDIO_FRAMES,
            "post-expiry audio consumed by the inbound pump",
        )

        await harness.driver.advance(0.0)
        await _finished(task)

    assert [pcm for _, _, pcm in first.sent_audio] == [_PRE_EXPIRY_AUDIO]
    assert _states(harness.connection) == ["connecting", "live", "ended"]
    assert _error_frames(harness.connection) == [
        ("auth_expired", AUTH_EXPIRED_MESSAGE)
    ]
    assert harness.connection.closes == [(CLOSE_CODE_ERROR, "auth_expired")]
    assert harness.store.sessions[_SESSION_ID].state is SessionState.ENDED
    assert len(_records_for(harness.handler, _AUTH_EXPIRED_EVENT)) == 1


async def test_stream_open_failure_reports_bedrock_unavailable() -> None:
    """A failed first stream open ends the session (Req 1.6).

    The session reports the ``error`` state, sends the
    ``bedrock_unavailable`` error frame, closes the connection, and
    persists as ``ERROR``; no pump ever starts, so no audio or
    transcript is processed.
    """
    failing = FakeBedrockStream(fail_open=True)
    harness = _make_harness(streams=(failing,))

    async with _running_session(harness) as task:
        harness.connection.feed(_session_start_frame())
        await _finished(task)

    assert _states(harness.connection) == ["connecting", "error"]
    assert _error_frames(harness.connection) == [
        ("bedrock_unavailable", BEDROCK_UNAVAILABLE_MESSAGE)
    ]
    assert harness.connection.closes == [(CLOSE_CODE_ERROR, "bedrock-unavailable")]
    assert harness.store.sessions[_SESSION_ID].state is SessionState.ERROR
    assert failing.opened_events == []
    assert harness.store.transcripts == {}
    assert len(_records_for(harness.handler, _STREAM_OPEN_FAILED_EVENT)) == 1


async def test_resume_with_unknown_session_id_is_rejected() -> None:
    """An unknown ``resumeSessionId`` is refused (Req 8.6).

    The client receives the ``session_not_found`` error frame, the
    connection closes with a failure code, no session state frame is
    ever sent, nothing is persisted, and the rejection is logged.
    """
    harness = _make_harness()

    async with _running_session(harness) as task:
        harness.connection.feed(
            _session_start_frame(resume_session_id="missing-session")
        )
        await _finished(task)

    assert _states(harness.connection) == []
    assert _error_frames(harness.connection) == [
        ("session_not_found", SESSION_NOT_FOUND_MESSAGE)
    ]
    assert harness.connection.closes == [(CLOSE_CODE_ERROR, "session-not-found")]
    assert harness.store.sessions == {}
    assert all(method != "put_session" for method, _ in harness.store.calls)
    assert len(_records_for(harness.handler, _RESUME_REJECTED_EVENT)) == 1


async def test_resume_of_terminal_session_is_rejected() -> None:
    """A ``resumeSessionId`` naming an ended session is refused (Req 8.6).

    A session that already reached a terminal state — the persisted
    shape of an expired session that has not yet been TTL-deleted — is
    rejected with the same ``session_not_found`` error frame, and the
    persisted record is left untouched.
    """
    harness = _make_harness()
    stale = VoiceSession(
        session_id="stale-session",
        engineer_id=_ENGINEER_ID,
        created_at=harness.clock.iso_now(),
        updated_at=harness.clock.iso_now(),
        state=SessionState.ENDED,
    )
    harness.store.sessions[stale.session_id] = stale

    async with _running_session(harness) as task:
        harness.connection.feed(
            _session_start_frame(resume_session_id=stale.session_id)
        )
        await _finished(task)

    assert _states(harness.connection) == []
    assert _error_frames(harness.connection) == [
        ("session_not_found", SESSION_NOT_FOUND_MESSAGE)
    ]
    assert harness.connection.closes == [(CLOSE_CODE_ERROR, "session-not-found")]
    assert harness.store.sessions[stale.session_id] is stale
    assert all(method != "put_session" for method, _ in harness.store.calls)
    assert len(_records_for(harness.handler, _RESUME_REJECTED_EVENT)) == 1


async def test_barge_in_relays_interrupted_frames_and_skips_transcript() -> None:
    """Nova Sonic barge-in signals become ``interrupted`` frames.

    Both interruption shapes — a ``textOutput`` whose content is the JSON
    interruption marker, and a ``contentEnd`` whose ``stopReason`` is
    ``INTERRUPTED`` — each relay one ``interrupted`` frame so the client
    flushes its playback queue. The marker is a control signal: it is
    never persisted to the Session_Store nor relayed as a ``transcript``
    frame, while an ordinary response line around it still is. Each
    barge-in is logged with the session identifier.
    """
    first = FakeBedrockStream()
    harness = _make_harness(streams=(first, FakeBedrockStream()))

    async with _running_session(harness) as task:
        await _go_live(harness)

        first.feed(_text_output_event(_PARTIAL_LINE))
        first.feed(
            {"type": "textOutput", "role": "ASSISTANT", "content": _INTERRUPTION_MARKER}
        )
        first.feed(
            {"type": "contentEnd", "contentType": "AUDIO", "stopReason": "INTERRUPTED"}
        )
        await _wait_for(
            lambda: _interrupted_frame_count(harness.connection) == 2,
            "both barge-in signals relayed as interrupted frames",
        )

        harness.connection.feed(_session_end_frame())
        await _finished(task)

    # The ordinary line was persisted and relayed; the marker was neither.
    transcripts = harness.store.transcripts.get(_SESSION_ID, [])
    assert [entry.text for entry in transcripts] == [_PARTIAL_LINE]
    relayed_texts = [
        frame.get("text")
        for frame in _frames(harness.connection)
        if frame.get("type") == "transcript"
    ]
    assert relayed_texts == [_PARTIAL_LINE]

    assert _error_frames(harness.connection) == []
    assert harness.connection.closes == [(CLOSE_CODE_NORMAL, "client_end")]
    assert len(_records_for(harness.handler, _INTERRUPTED_EVENT)) == 2
    interrupted_records = _records_for(harness.handler, _INTERRUPTED_EVENT)
    assert all(
        getattr(record, "session_id", None) == _SESSION_ID
        for record in interrupted_records
    )


def _text_input_frame(text: str) -> str:
    """Build a ``text.input`` client frame carrying a typed request.

    Args:
        text: The typed request text.

    Returns:
        The frame's wire JSON text.
    """
    return json.dumps({"type": "text.input", "text": text})


async def test_typed_request_enters_the_conversation_and_is_persisted() -> None:
    """A ``text.input`` frame becomes a USER text block on the live stream.

    Typing complements speaking (identifiers are easier typed than
    pronounced), so a typed request joins the same conversation: it is
    persisted as a transcript entry, echoed to the client as a
    ``transcript`` frame so it appears in the engineer's own pane and
    survives reconnect, and forwarded to the live stream as the three
    events of a USER text content block — which Nova Sonic answers by
    voice exactly as it answers speech.
    """
    first = FakeBedrockStream()
    harness = _make_harness(streams=(first, FakeBedrockStream()))

    async with _running_session(harness) as task:
        await _go_live(harness)
        sent_before = len(first.sent_events)

        harness.connection.feed(_text_input_frame(_TYPED_REQUEST))
        await _wait_for(
            lambda: len(first.sent_events) >= sent_before + 3,
            "typed request forwarded as a text content block",
        )

        harness.connection.feed(_session_end_frame())
        await _finished(task)

    block = first.sent_events[sent_before : sent_before + 3]
    assert [event["type"] for event in block] == [
        "contentStart",
        "textInput",
        "contentEnd",
    ]
    assert block[0]["contentType"] == "TEXT"
    assert block[0]["role"] == Role.USER.value
    assert block[1]["content"] == _TYPED_REQUEST
    # One content name for the whole block, unique within the stream.
    assert block[0]["contentName"] == block[1]["contentName"] == block[2]["contentName"]

    # Persisted once, as an engineer utterance (Req 8.2).
    entries = harness.store.transcripts[_SESSION_ID]
    assert [(entry.role, entry.text) for entry in entries] == [
        (Role.USER, _TYPED_REQUEST)
    ]

    # Echoed to the client so the engineer's pane shows what was sent.
    echoed = [
        frame for frame in _frames(harness.connection) if frame.get("type") == "transcript"
    ]
    assert echoed == [
        {
            "type": "transcript",
            "role": "user",
            "text": _TYPED_REQUEST,
            "timestamp": entries[0].timestamp,
        }
    ]
    assert _error_frames(harness.connection) == []
    assert harness.connection.closes == [(CLOSE_CODE_NORMAL, "client_end")]


async def test_blank_typed_request_is_rejected_at_the_wire() -> None:
    """A blank ``text.input`` frame is ignored, not forwarded.

    A blank typed request carries no request at all, so frame parsing
    rejects it and the malformed-frame path logs and ignores it: nothing
    reaches the stream, nothing is persisted, and the session continues.
    """
    first = FakeBedrockStream()
    harness = _make_harness(streams=(first, FakeBedrockStream()))

    async with _running_session(harness) as task:
        await _go_live(harness)
        sent_before = len(first.sent_events)

        harness.connection.feed(_text_input_frame("   "))
        await _wait_for(
            lambda: len(_records_for(harness.handler, _PROTOCOL_VIOLATION_EVENT)) == 1,
            "blank typed request rejected as a protocol violation",
        )

        harness.connection.feed(_session_end_frame())
        await _finished(task)

    assert len(first.sent_events) == sent_before
    assert harness.store.transcripts.get(_SESSION_ID, []) == []
    assert _error_frames(harness.connection) == []


async def test_keepalive_sends_silence_when_client_input_goes_idle() -> None:
    """Idle client input is covered by keepalive silence, not a dead stream.

    Nova Sonic closes a stream that receives neither audio nor interactive
    content for 295 seconds — observed in production when a backgrounded
    browser tab throttled the capture worklet, surfacing to the engineer as
    an unexplained internal error. After ``KEEPALIVE_IDLE_SECONDS`` of
    silence the keepalive sends one silent frame, which resets the
    service-side timer; client audio resets the idle interval, so an active
    speaker never triggers it.
    """
    first = FakeBedrockStream()
    harness = _make_harness(streams=(first, FakeBedrockStream()))

    async with _running_session(harness) as task:
        await _go_live(harness)
        await _wait_for(
            lambda: harness.driver.pending == _MANAGER_TIMER_COUNT,
            "manager timers armed",
        )
        assert first.sent_audio == []

        # Well inside the idle window: nothing is sent.
        await harness.driver.advance(KEEPALIVE_IDLE_SECONDS / 2)
        assert first.sent_audio == []

        # Past the window: exactly one silent frame holds the stream open.
        await harness.driver.advance(KEEPALIVE_IDLE_SECONDS)
        await _wait_for(
            lambda: len(first.sent_audio) == 1, "keepalive silence sent"
        )
        _, _, silence = first.sent_audio[0]
        assert set(silence) == {0}

        harness.connection.feed(_session_end_frame())
        await _finished(task)

    assert _error_frames(harness.connection) == []
    assert len(_records_for(harness.handler, _KEEPALIVE_EVENT)) >= 1
    # Silence never reaches the engineer's transcript.
    assert harness.store.transcripts.get(_SESSION_ID, []) == []


async def test_idle_warning_precedes_idle_timeout_close() -> None:
    """A quiet engineer is warned first, then the session ends.

    Bounding a session by engineer activity is what replaced bounding it
    by the presented token's remaining lifetime: a session inherits
    whatever is left of a 60-minute access token, so one started late in
    that window used to be killed after minutes. Here the token is valid
    throughout, and idleness alone drives the outcome: no notice before
    the warning period, one advisory ``idle_warning`` frame at it (the
    session stays live), and an ``idle_timeout`` frame plus a *normal*
    close once the grace period also elapses — an idle close is policy,
    not failure.
    """
    # A 15-minute idle session outlives several 7 m 30 s rollovers, so the
    # factory must have streams for them; segmentation is incidental here
    # and only shows up as extra states in the status sequence.
    harness = _make_harness(streams=tuple(FakeBedrockStream() for _ in range(6)))

    async with _running_session(harness) as task:
        await _go_live(harness)
        await _wait_for(
            lambda: harness.driver.pending == _MANAGER_TIMER_COUNT,
            "manager timers armed",
        )

        # Idle 300 s — comfortably inside the warning period, so the
        # engineer is left alone.
        await harness.driver.advance(DEFAULT_IDLE_WARNING_SECONDS / 2)
        assert _error_frames(harness.connection) == []

        # Idle 601 s — past the warning period but short of the deadline
        # (900 s): exactly one advisory notice, and the session is still
        # running (no close, no terminal state).
        await harness.driver.advance(DEFAULT_IDLE_WARNING_SECONDS / 2 + 1.0)
        await _wait_for(
            lambda: _error_frames(harness.connection)
            == [("idle_warning", IDLE_WARNING_MESSAGE)],
            "idle warning sent",
        )
        assert harness.connection.closes == []
        assert _states(harness.connection)[-1] == "live"
        assert _idle_warning_recoverable_flags(harness.connection) == [True]

        # Idle 901 s — still quiet through the grace period, so the
        # session ends.
        await harness.driver.advance(DEFAULT_IDLE_GRACE_SECONDS)
        await _finished(task)

    assert _error_frames(harness.connection) == [
        ("idle_warning", IDLE_WARNING_MESSAGE),
        ("idle_timeout", IDLE_TIMEOUT_MESSAGE),
    ]
    assert harness.connection.closes == [(CLOSE_CODE_NORMAL, "idle_timeout")]
    states = _states(harness.connection)
    assert states[0] == "connecting"
    assert states[-1] == "ended"
    assert harness.store.sessions[_SESSION_ID].state is SessionState.ENDED
    assert len(_records_for(harness.handler, _IDLE_WARNING_EVENT)) == 1
    assert len(_records_for(harness.handler, _IDLE_TIMEOUT_EVENT)) == 1


async def test_engineer_turn_after_warning_keeps_session_and_rearms() -> None:
    """Speaking after the warning saves the session and re-arms the warning.

    A recognized spoken turn is the only signal that separates a talking
    engineer from an open microphone in a silent room, so it — not the
    arrival of audio frames — clears the idle interval. After the turn the
    session survives the grace period it was inside, and a later quiet
    spell warns again rather than passing silently into a close.
    """
    harness = _make_harness(streams=tuple(FakeBedrockStream() for _ in range(6)))

    async with _running_session(harness) as task:
        await _go_live(harness)
        await _wait_for(
            lambda: harness.driver.pending == _MANAGER_TIMER_COUNT,
            "manager timers armed",
        )

        # Idle 601 s — the warning fires.
        await harness.driver.advance(DEFAULT_IDLE_WARNING_SECONDS + 1.0)
        await _wait_for(
            lambda: len(_error_frames(harness.connection)) == 1,
            "first idle warning sent",
        )

        # The engineer answers: ASR emits their turn as a USER textOutput,
        # which resets the idle interval to zero. It is fed to whichever
        # stream is live now, since a rollover has already replaced the
        # first one by this point.
        live = harness.factory.created[-1]
        assert isinstance(live, FakeBedrockStream)
        live.feed({"type": "textOutput", "role": "USER", "content": _ENGINEER_TURN})
        await _wait_for(
            lambda: harness.store.transcripts.get(_SESSION_ID, []) != [],
            "engineer turn persisted",
        )

        # Idle 301 s since the turn — longer than the grace period the
        # session was inside when the turn landed, so this proves that
        # grace period was voided rather than merely deferred.
        await harness.driver.advance(DEFAULT_IDLE_GRACE_SECONDS + 1.0)
        assert harness.connection.closes == []
        assert len(_error_frames(harness.connection)) == 1

        # Idle 601 s since the turn — quiet again, so the warning re-arms
        # rather than staying spent.
        await harness.driver.advance(DEFAULT_IDLE_WARNING_SECONDS / 2)
        await _wait_for(
            lambda: len(_error_frames(harness.connection)) == 2,
            "idle warning re-armed after the engineer turn",
        )
        assert harness.connection.closes == []

        harness.connection.feed(_session_end_frame())
        await _finished(task)

    assert _error_frames(harness.connection) == [
        ("idle_warning", IDLE_WARNING_MESSAGE),
        ("idle_warning", IDLE_WARNING_MESSAGE),
    ]
    assert harness.connection.closes == [(CLOSE_CODE_NORMAL, "client_end")]
    assert len(_records_for(harness.handler, _IDLE_WARNING_EVENT)) == 2
    assert _records_for(harness.handler, _IDLE_TIMEOUT_EVENT) == []
