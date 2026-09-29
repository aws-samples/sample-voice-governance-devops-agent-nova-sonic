"""Voice session manager: per-connection orchestration of one Voice_Session.

Implements the design's ``voice_session_manager`` component — the wiring
between one WebSocket connection, the Bedrock_Stream(s) serving it, the
tool router, and the Session_Store. Each connection is served by one
:class:`asyncio.TaskGroup` holding four long-lived tasks:

- **Inbound pump**: relays client binary PCM to the live stream
  immediately on receipt — well inside the 500 ms forwarding bound
  (Req 1.2) — or into the bounded FIFO audio buffer while a segmentation
  rollover is in progress (Req 2.4); parses client control frames and
  turns ``session.end`` into a graceful end (Req 2.5).
- **Outbound pump**: relays ``audioOutput`` events as binary frames
  (Req 1.3), ``textOutput`` events as ``transcript`` frames after the
  entry is appended to the accumulator and durably persisted under the
  bounded retry policy (Req 1.4, 2.7, 8.2), and spawns one task per
  ``toolUse`` event that routes through the tool router and returns the
  tool result into the stream (Req 3.4).
- **Segmentation timer**: rolls the session onto a fresh Bedrock_Stream
  when the current stream's age reaches the rollover threshold (7 m 30 s
  by default, Req 2.2): drain the old stream, replay the full context via
  ``domain.segmentation.build_replay_sequence``, reopen the interactive
  audio block, and flush the buffered audio FIFO in arrival order
  (Req 2.3, 2.4) — all inside the 10-second watchdog budget; a failure or
  timeout persists the partial transcript (entries are persisted
  incrementally, so nothing is pending), reports ``segmentation_failed``,
  and logs with the Voice_Session identifier (Req 2.6).
- **Token-expiry watchdog**: when the validated token's ``expires_at``
  passes mid-session, sends an ``auth_expired`` error frame, closes the
  session, and never processes audio received after the expiry (Req 7.6;
  the inbound pump independently drops post-expiry audio).

**Persist-before-confirm (Req 8.2, design Property 11)**: every
Session_Store write completes before the matching state change is
reported. Session state frames are sent only after ``put_session``
returned (Req 9.5), transcript frames only after ``append_transcript``
survived its bounded retries (Req 2.7) — an entry whose persistence
exhausted all retries is logged with the session id and its frame is
withheld. The in-memory session snapshot is adopted only after its
persist succeeded, so the local state never runs ahead of the store.

**Lifecycle**: the first client frame must be ``session.start``. A
``resumeSessionId`` restores the persisted session snapshot and all
transcript entries from the Session_Store and replays them into a fresh
Bedrock_Stream — the same replay mechanism segmentation uses (Req 8.3);
an unknown, expired, or terminal session — or one owned by a different
engineer — is rejected with ``session_not_found`` (Req 8.6). A stream
that cannot be opened ends the session with ``bedrock_unavailable``
before the close (Req 1.6). Incident-scoped sessions inject the incident
summary and severity into the system prompt (Req 5.8). A graceful end
tears the stream down (``promptEnd``/``sessionEnd`` in the adapter),
persists the session as ``ENDED``, reports the ``ended`` state, and
closes the WebSocket (Req 2.5). Live-session accounting is reported to
the protection manager, and every session registers a drain terminator
that delivers ``session.terminating`` before close when the drain period
expires (Req 10.8).

**Transport abstraction**: the manager drives the minimal
:class:`VoiceConnection` protocol instead of a FastAPI WebSocket, so
this module imports no web framework and no SDK (Req 17.6). The
composition root adapts the real WebSocket to the protocol and
translates its disconnect exception into :class:`ConnectionClosed`.

**Cancellation discipline**: pumps convert every expected failure into
the private :class:`_SessionEnd` control signal, which ends the task
group and cancels the sibling tasks cleanly; only truly unexpected
exceptions escape to the composition root's boundary handler. Every log
entry is produced inside a ``bind_session`` scope, so it carries the
Voice_Session identifier (Req 19.4).
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum, unique
from functools import partial
from typing import Final, Protocol

from shared.exceptions import PortalError
from shared.retry import retry_async

from app.auth.jwt_validator import ValidatedToken
from app.domain.audio_buffer import BoundedAudioBuffer
from app.domain.segmentation import (
    ASK_DEVOPS_AGENT_TOOL_NAME,
    ASK_DEVOPS_AGENT_TOOL_SPEC,
    ROLLOVER_STREAM_AGE_SECONDS,
    WATCHDOG_BUDGET_SECONDS,
    build_replay_sequence,
    build_text_input_block,
    seconds_until_rollover,
)
from app.domain.session import (
    SessionEvent,
    SessionState,
    VoiceSession,
    create_session,
    transition,
)
from app.domain.transcript import Role, TranscriptAccumulator
from app.domain.ttl import DEFAULT_RETENTION_DAYS, compute_ttl_from_epoch
from app.exceptions import BedrockStreamError, SessionStoreError
from app.logging import bind_session
from app.orchestration.drain_manager import DrainManager
from app.orchestration.protection_manager import ProtectionManager
from app.orchestration.tool_router import ToolRouter
from app.ports.bedrock_stream import BedrockStreamPort
from app.ports.session_store import SessionStorePort
from app.protocol.ws_messages import (
    ErrorCategory,
    ErrorFrame,
    InterruptedFrame,
    ProtocolError,
    ServerFrame,
    SessionEndFrame,
    SessionStartFrame,
    SessionStateFrame,
    SessionTerminatingFrame,
    TextInputFrame,
    TranscriptFrame,
    parse_client_frame,
    serialize_server_frame,
)

__all__ = [
    "AUTH_EXPIRED_MESSAGE",
    "BEDROCK_UNAVAILABLE_MESSAGE",
    "CLOSE_CODE_ERROR",
    "CLOSE_CODE_GOING_AWAY",
    "CLOSE_CODE_NORMAL",
    "DRAIN_TERMINATING_REASON",
    "INTERNAL_ERROR_MESSAGE",
    "SEGMENTATION_FAILED_MESSAGE",
    "SESSION_NOT_FOUND_MESSAGE",
    "START_EXPECTED_MESSAGE",
    "TRANSCRIPT_PERSIST_RETRIES",
    "ConnectionClosed",
    "VoiceConnection",
    "VoiceSessionManager",
]

CLOSE_CODE_NORMAL: Final = 1000
"""WebSocket close code for a gracefully ended session (Req 2.5)."""

CLOSE_CODE_GOING_AWAY: Final = 1001
"""WebSocket close code used when the drain period terminates a session."""

CLOSE_CODE_ERROR: Final = 1011
"""WebSocket close code for every failure-driven close."""

DRAIN_TERMINATING_REASON: Final = "drain_timeout"
"""``reason`` carried by the ``session.terminating`` drain notice (Req 10.8)."""

TRANSCRIPT_PERSIST_RETRIES: Final = 3
"""Bounded retry budget for one transcript persistence operation (Req 2.7)."""

AUTH_EXPIRED_MESSAGE: Final = (
    "The authentication token expired; the session is closed."
)
"""``auth_expired`` error-frame message for mid-session expiry (Req 7.6)."""

BEDROCK_UNAVAILABLE_MESSAGE: Final = (
    "A speech stream could not be opened; the session is closed."
)
"""``bedrock_unavailable`` error-frame message for an open failure (Req 1.6)."""

SEGMENTATION_FAILED_MESSAGE: Final = (
    "The session could not be carried over to a fresh speech stream."
)
"""``segmentation_failed`` error-frame message for a failed rollover (Req 2.6)."""

SESSION_NOT_FOUND_MESSAGE: Final = (
    "The requested session is not available to resume."
)
"""``session_not_found`` error-frame message for a rejected resume (Req 8.6)."""

IDLE_WARNING_MESSAGE: Final = (
    "You have been quiet for a while. Speak or type to carry on — "
    "otherwise this session will end in 5 minutes."
)
"""``idle_warning`` notice sent after the idle warning period elapses.

Names the grace period in minutes so the engineer can act on it without
consulting the UI; the wording matches
:data:`DEFAULT_IDLE_GRACE_SECONDS`.
"""

IDLE_TIMEOUT_MESSAGE: Final = (
    "The session ended because no request was received after the idle warning."
)
"""``idle_timeout`` error-frame message sent before an idle close."""

INTERNAL_ERROR_MESSAGE: Final = "An internal error ended the session."
"""``internal`` error-frame message for unexpected failures."""

START_EXPECTED_MESSAGE: Final = (
    "The first frame must be a session.start control frame."
)
"""``internal`` error-frame message when the opening frame is not ``session.start``."""

_NO_STREAM_DETAIL: Final = "no-open-stream"
"""``BedrockStreamError`` detail used when no stream is open yet."""

_EVENT_SESSION_STARTED: Final = "session.started"
"""``event`` log field value when a session enters its lifecycle."""

_EVENT_SESSION_ENDED: Final = "session.ended"
"""``event`` log field value when a session's terminal sequence completes."""

_EVENT_STREAM_OPEN_FAILED: Final = "session.stream_open_failed"
"""``event`` log field value when a Bedrock_Stream cannot be opened (Req 1.6)."""

_EVENT_STREAM_FAILED: Final = "session.stream_failed"
"""``event`` log field value when the live Bedrock_Stream fails mid-session."""

_EVENT_SEGMENTATION_STARTED: Final = "session.segmentation_started"
"""``event`` log field value when a segmentation rollover begins (Req 2.2)."""

_EVENT_SEGMENTATION_COMPLETED: Final = "session.segmentation_completed"
"""``event`` log field value when a segmentation rollover completes."""

_EVENT_SEGMENTATION_FAILED: Final = "session.segmentation_failed"
"""``event`` log field value when a segmentation rollover fails (Req 2.6)."""

_EVENT_AUTH_EXPIRED: Final = "session.auth_expired"
"""``event`` log field value when the token-expiry watchdog fires (Req 7.6)."""

_EVENT_AUDIO_DROPPED: Final = "session.audio_dropped"
"""``event`` log field value when the segmentation buffer drops a frame (Req 2.4)."""

_EVENT_RESUME_REJECTED: Final = "session.resume_rejected"
"""``event`` log field value when a ``resumeSessionId`` is rejected (Req 8.6)."""

_EVENT_TOOL_RESULT_DROPPED: Final = "session.tool_result_dropped"
"""``event`` log field value when a tool result cannot be delivered."""

_EVENT_PROTOCOL_VIOLATION: Final = "session.protocol_violation"
"""``event`` log field value for a malformed or unexpected client frame."""

_EVENT_INTERRUPTED: Final = "session.interrupted"
"""``event`` log field value when the engineer barges in on a response."""

_EVENT_TEXT_INPUT: Final = "session.text_input"
"""``event`` log field value when a typed engineer request is forwarded."""

_EVENT_KEEPALIVE: Final = "session.keepalive"
"""``event`` log field value when silence is sent to hold the stream open."""

_EVENT_IDLE_WARNING: Final = "session.idle_warning"
"""``event`` log field value when the idle warning notice is sent."""

_EVENT_IDLE_TIMEOUT: Final = "session.idle_timeout"
"""``event`` log field value when an idle session is ended."""

BEDROCK_INPUT_GAP_LIMIT_SECONDS: Final = 295.0
"""Nova Sonic's maximum tolerated gap between input events.

The service closes a stream with ``ValidationException: Timed out waiting
for audio bytes or interactive content. Please ensure gaps between audio
bytes and interactive content are less than 295 seconds.`` — observed in
production when a browser tab was backgrounded (which throttles the
capture AudioWorklet) and no PCM reached the service for five minutes.
"""

KEEPALIVE_IDLE_SECONDS: Final = 120.0
"""Input silence tolerated before the keepalive sends a silent frame.

Less than half of :data:`BEDROCK_INPUT_GAP_LIMIT_SECONDS`, so a single
missed keepalive tick still leaves the stream well inside the service
limit.
"""

KEEPALIVE_POLL_SECONDS: Final = 15.0
"""How often the keepalive task re-checks the idle interval.

Public so test harnesses that route the manager's injected sleep by
requested delay can recognize the keepalive's own sleeper.
"""

_KEEPALIVE_SILENCE_BYTES: Final = 3200
"""One silent frame: 100 ms of 16 kHz 16-bit mono PCM (Req 1.1 format)."""

DEFAULT_IDLE_WARNING_SECONDS: Final = 600.0
"""Engineer silence tolerated before the idle warning notice is sent.

Measured between *engineer turns*, not audio frames: the microphone
streams PCM continuously while capture is on, so
:attr:`_SessionRuntime._last_input_at` (which the keepalive uses) never
goes stale on a live connection and cannot detect a quiet engineer. Only
a recognized spoken turn or a typed request counts as activity.
"""

DEFAULT_IDLE_GRACE_SECONDS: Final = 300.0
"""Further silence tolerated after the warning before the session ends.

A turn during this window clears the idle interval and re-arms the
warning, so the engineer never loses a session they came back to.
"""

IDLE_POLL_SECONDS: Final = 15.0
"""How often the idle watchdog re-checks the engineer-silence interval.

Public so test harnesses that route the manager's injected sleep by
requested delay can recognize the idle watchdog's own sleeper, matching
:data:`KEEPALIVE_POLL_SECONDS`.
"""

_TEXT_INPUT_CONTENT_NAME_PREFIX: Final = "text-input-"
"""Prefix of typed-request content-block names; a 1-based counter follows."""


def _is_interruption_marker(content: object) -> bool:
    """Report whether a ``textOutput`` content value is the barge-in marker.

    Nova Sonic signals an engineer interruption by emitting a
    ``textOutput`` event whose content is a small JSON object carrying
    ``"interrupted": true`` (for example ``{ "interrupted" : true }``)
    instead of response text. Such content is a control signal, never a
    transcript line.

    Args:
        content: The ``content`` value of a ``textOutput`` event.

    Returns:
        ``True`` when the content parses as a JSON object whose
        ``interrupted`` member is ``true``; ``False`` for ordinary
        transcript text, non-string content, and malformed JSON.
    """
    if not isinstance(content, str) or _INTERRUPTION_MARKER_KEY not in content:
        return False
    try:
        parsed: object = json.loads(content)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get(_INTERRUPTION_MARKER_KEY) is True

_EVENT_PERSIST_FAILED: Final = "session.persist_failed"
"""``event`` log field value when a session-state persist fails (Req 8.5)."""

_TEXT_OUTPUT_EVENT: Final = "textOutput"
"""Stream output event carrying an ASR or response transcript (Req 1.4)."""

_AUDIO_OUTPUT_EVENT: Final = "audioOutput"
"""Stream output event carrying base64 response audio (Req 1.3)."""

_TOOL_USE_EVENT: Final = "toolUse"
"""Stream output event invoking the ``ask_devops_agent`` tool (Req 3.4)."""

_CONTENT_END_EVENT: Final = "contentEnd"
"""Stream output event closing one content block; carries ``stopReason``."""

_INTERRUPTED_STOP_REASON: Final = "INTERRUPTED"
"""``contentEnd`` stop reason Nova Sonic reports on engineer barge-in."""

_INTERRUPTION_MARKER_KEY: Final = "interrupted"
"""Key of the JSON interruption marker Nova Sonic emits as ``textOutput``."""

_MODULE_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class ConnectionClosed(PortalError):
    """The WebSocket peer is gone and the connection can carry no more frames.

    Raised by :class:`VoiceConnection` implementations — never by this
    module — when a receive or send hits a closed connection. The
    composition root's adapter translates the web framework's disconnect
    exception into this class so the manager stays framework-free
    (Req 17.6). Extends the shared ``PortalError`` hierarchy (Req 17.4)
    and is defined here because it belongs to the transport contract this
    module owns.

    Attributes:
        detail: Short description of the close condition, when known.
    """

    def __init__(self, detail: str | None = None) -> None:
        """Initialize the error with an optional close description.

        Args:
            detail: Short description of the close condition, for example
                the WebSocket close code; ``None`` when unknown.
        """
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"WebSocket connection closed{suffix}")


class VoiceConnection(Protocol):
    """Minimal WebSocket-like transport the session manager drives.

    Structural protocol implemented by the composition root's adapter
    over the accepted WebSocket (and by the scripted fake connection in
    tests), keeping this module free of any web-framework import
    (Req 17.6). Implementations translate their framework's disconnect
    exception into :class:`ConnectionClosed`, and after :meth:`close` has
    run, pending and subsequent :meth:`receive` calls raise
    :class:`ConnectionClosed` so the pumps unwind.
    """

    async def send_text(self, data: str) -> None:
        """Send one text frame to the client.

        Args:
            data: Serialized JSON control frame.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        ...

    async def send_bytes(self, data: bytes) -> None:
        """Send one binary frame (raw 24 kHz PCM audio) to the client.

        Args:
            data: Raw PCM audio bytes (Req 1.3).

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        ...

    async def receive(self) -> str | bytes:
        """Await the next frame from the client.

        Returns:
            The frame payload: ``str`` for text control frames, ``bytes``
            for binary PCM audio (Req 1.1).

        Raises:
            ConnectionClosed: When the client disconnected or the
                connection was closed locally.
        """
        ...

    async def close(self, code: int, reason: str) -> None:
        """Close the connection; closing an already-closed connection is safe.

        Args:
            code: WebSocket close code.
            reason: Short close reason token.
        """
        ...


@unique
class _EndReason(StrEnum):
    """Why a Voice_Session's task group stopped pumping.

    The member values are the lowercase tokens recorded in the session-end
    log entry and used as the WebSocket close reason.
    """

    CLIENT_END = "client_end"
    """The engineer sent ``session.end`` (Req 2.5)."""

    CLIENT_DISCONNECT = "client_disconnect"
    """The WebSocket closed or dropped (design: ``LIVE → ENDED`` on WS close)."""

    STREAM_ENDED = "stream_ended"
    """The live Bedrock_Stream completed on its own outside a rollover."""

    AUTH_EXPIRED = "auth_expired"
    """The Cognito token expired mid-session (Req 7.6)."""

    IDLE_TIMEOUT = "idle_timeout"
    """No engineer turn through the idle warning and its grace period."""

    SEGMENTATION_FAILED = "segmentation_failed"
    """A segmentation rollover failed or overran its watchdog (Req 2.6)."""

    STREAM_FAILED = "stream_failed"
    """The live Bedrock_Stream failed mid-session outside a rollover."""

    INTERNAL = "internal"
    """A session-state persist or another internal step failed."""

    @property
    def is_graceful(self) -> bool:
        """Return whether this end closes the WebSocket with a normal code.

        Returns:
            ``True`` for the ends that are part of normal operation
            (client end, client disconnect, stream completion), ``False``
            for every failure-driven end.
        """
        return self in {
            _EndReason.CLIENT_END,
            _EndReason.CLIENT_DISCONNECT,
            _EndReason.STREAM_ENDED,
            # An idle close is policy, not failure: the engineer was
            # warned and chose not to continue, so it closes 1000 like a
            # deliberate end rather than 1011.
            _EndReason.IDLE_TIMEOUT,
        }


class _SessionEnd(Exception):
    """Control-flow signal that ends the per-session task group.

    Raised inside a pump task when the session must stop; the enclosing
    :class:`asyncio.TaskGroup` cancels the sibling tasks and the runtime
    extracts the reason to drive the terminal sequence. Never escapes
    :meth:`VoiceSessionManager.run_session`. Deliberately outside the
    ``PortalError`` hierarchy: it reports no failure, it steers control.

    Attributes:
        reason: Why the session is ending.
    """

    def __init__(self, reason: _EndReason) -> None:
        """Initialize the signal with its end reason.

        Args:
            reason: Why the session is ending.
        """
        self.reason = reason
        super().__init__(reason.value)


def _first_reason(group: BaseExceptionGroup[_SessionEnd]) -> _EndReason:
    """Extract the end reason from the first ``_SessionEnd`` leaf of a group.

    ``except*`` guarantees every leaf of the matched group is a
    ``_SessionEnd``; when several pumps ended the session in the same
    event-loop step, the first collected reason wins.

    Args:
        group: The exception group matched by ``except* _SessionEnd``.

    Returns:
        The reason of the first ``_SessionEnd`` found, or
        ``_EndReason.INTERNAL`` for a structurally empty group (which
        ``except*`` never produces).
    """
    for exc in group.exceptions:
        if isinstance(exc, _SessionEnd):
            return exc.reason
        if isinstance(exc, BaseExceptionGroup):
            return _first_reason(exc)
    return _EndReason.INTERNAL


def _terminal_event_for(state: SessionState) -> SessionEvent | None:
    """Pick the legal state-machine edge that ends a session from ``state``.

    The design state machine ends a ``LIVE`` session via ``SESSION_ENDED``
    and offers ``SEGMENTING`` no edge to ``ENDED`` — a session that stops
    mid-rollover ends through ``SEGMENTATION_FAILED`` (the rollover did
    not complete, Req 2.6).

    Args:
        state: The session's current state.

    Returns:
        The event to raise, or ``None`` when the state is already
        terminal (or still ``CONNECTING``, whose failure edge is raised
        explicitly by the stream-open failure path).
    """
    if state is SessionState.LIVE:
        return SessionEvent.SESSION_ENDED
    if state is SessionState.SEGMENTING:
        return SessionEvent.SEGMENTATION_FAILED
    return None


def _error_frame_for(reason: _EndReason) -> ErrorFrame | None:
    """Build the error frame a failure-driven end owes the client.

    Args:
        reason: Why the session ended.

    Returns:
        The ``auth_expired`` frame for a mid-session token expiry
        (Req 7.6), the ``idle_timeout`` frame for an idle close, the
        ``segmentation_failed`` frame for a failed rollover (Req 2.6),
        the ``internal`` frame for stream or persist failures, or
        ``None`` for the ends the engineer already knows about (they
        asked for it, they vanished, or the stream completed), which owe
        no frame.

        Note that owing a frame and closing 1011 are independent: an
        idle close is graceful (1000) yet still explains itself, because
        the engineer did not ask for it.
    """
    if reason is _EndReason.AUTH_EXPIRED:
        return ErrorFrame(
            category=ErrorCategory.AUTH_EXPIRED,
            message=AUTH_EXPIRED_MESSAGE,
            recoverable=False,
        )
    if reason is _EndReason.IDLE_TIMEOUT:
        return ErrorFrame(
            category=ErrorCategory.IDLE_TIMEOUT,
            message=IDLE_TIMEOUT_MESSAGE,
            recoverable=False,
        )
    if reason is _EndReason.SEGMENTATION_FAILED:
        return ErrorFrame(
            category=ErrorCategory.SEGMENTATION_FAILED,
            message=SEGMENTATION_FAILED_MESSAGE,
            recoverable=False,
        )
    if reason in {_EndReason.STREAM_FAILED, _EndReason.INTERNAL}:
        return ErrorFrame(
            category=ErrorCategory.INTERNAL,
            message=INTERNAL_ERROR_MESSAGE,
            recoverable=False,
        )
    return None


def _default_id_factory() -> str:
    """Return a fresh UUIDv4 identifier in hex form.

    Returns:
        The 32-character lowercase hex digest of a new UUIDv4, used for
        session identifiers, prompt names, and content names.
    """
    return uuid.uuid4().hex


class VoiceSessionManager:
    """Serves ``/ws/voice`` connections end to end (design voice_session_manager).

    One instance serves every connection of the process: all per-session
    state lives in a private per-connection runtime, so concurrent
    :meth:`run_session` calls never interfere. External reach is confined
    to the injected ports and collaborators (Req 17.6): a fresh
    ``BedrockStreamPort`` per Bedrock_Stream from ``stream_factory``
    (segmentation and reconnect open new instances, Req 2.2, 8.3), the
    Session_Store for persist-before-confirm state handling (Req 8.2),
    the tool router for ``toolUse`` dispatch, the protection manager for
    live-session accounting (Req 10.3), and the drain manager for
    termination notices (Req 10.8).
    """

    def __init__(
        self,
        *,
        stream_factory: Callable[[], BedrockStreamPort],
        store: SessionStorePort,
        tool_router: ToolRouter,
        protection: ProtectionManager,
        drain: DrainManager,
        system_prompt: str,
        rollover_seconds: float = ROLLOVER_STREAM_AGE_SECONDS,
        watchdog_seconds: float = WATCHDOG_BUDGET_SECONDS,
        idle_warning_seconds: float = DEFAULT_IDLE_WARNING_SECONDS,
        idle_grace_seconds: float = DEFAULT_IDLE_GRACE_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        transcript_retries: int = TRANSCRIPT_PERSIST_RETRIES,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Initialize the manager with its collaborators and policies.

        Args:
            stream_factory: Zero-argument factory returning a fresh
                ``BedrockStreamPort`` — one port instance per
                Bedrock_Stream, so segmentation rollovers and reconnects
                call it again (Req 2.2, 8.3).
            store: Session_Store port for sessions and transcripts
                (Req 8.1).
            tool_router: Router handling every ``toolUse`` event through
                the guardrail gate and the DevOps Agent (Req 3.4, 4.1).
            protection: Live-session registry driving ECS scale-in
                protection (Req 10.3).
            drain: Drain scheduler this manager registers per-session
                terminators with (Req 10.8).
            system_prompt: System-prompt template establishing the
                read-only diagnostic persona; incident-scoped sessions
                get the incident summary and severity appended (Req 5.8).
            rollover_seconds: Stream age that triggers segmentation;
                defaults to the design's 7 m 30 s (Req 2.2), mirroring
                ``Settings.segmentation_rollover_seconds``.
            watchdog_seconds: Budget for one segmentation rollover to
                complete; defaults to the design's 10 s (Req 2.6),
                mirroring ``Settings.segmentation_watchdog_seconds``.
            idle_warning_seconds: Engineer silence tolerated before the
                ``idle_warning`` notice; mirrors
                ``Settings.idle_warning_seconds``.
            idle_grace_seconds: Further silence tolerated after the
                warning before the session ends; mirrors
                ``Settings.idle_grace_seconds``.
            retention_days: Session_Store TTL retention period in days
                (Req 8.4), mirroring ``Settings.retention_days``.
            transcript_retries: Bounded retry budget per transcript
                persistence operation; the design's 3 retries (Req 2.7).
            clock: Wall clock in epoch seconds for timestamps, TTLs, and
                the token-expiry deadline; defaults to ``time.time``.
                Tests inject a fake for determinism.
            monotonic: Monotonic clock in seconds for stream-age
                measurement; defaults to ``time.monotonic``.
            sleep: Awaitable delay used by the segmentation timer, the
                token watchdog, and retry backoff; defaults to
                ``asyncio.sleep``. Tests inject a fake to drive timers.
            logger: Logger for session lifecycle entries; this module's
                logger when omitted. Entries are emitted inside
                ``bind_session`` scopes, so they carry the session id
                (Req 19.4).
            id_factory: Factory for session identifiers, prompt names,
                and content names; fresh UUIDv4 hex strings when omitted.
        """
        self._stream_factory = stream_factory
        self._store = store
        self._tool_router = tool_router
        self._protection = protection
        self._drain = drain
        self._system_prompt = system_prompt
        self._rollover_seconds = rollover_seconds
        self._watchdog_seconds = watchdog_seconds
        self._idle_warning_seconds = idle_warning_seconds
        self._idle_grace_seconds = idle_grace_seconds
        self._retention_days = retention_days
        self._transcript_retries = transcript_retries
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._logger = logger if logger is not None else _MODULE_LOGGER
        self._id_factory: Callable[[], str] = (
            id_factory if id_factory is not None else _default_id_factory
        )

    async def run_session(
        self, connection: VoiceConnection, token: ValidatedToken
    ) -> None:
        """Serve one accepted WebSocket connection for its whole lifetime.

        Called by the composition root after the JWT was validated and
        the connection accepted (Req 7.2). Returns when the session ended
        and the connection was closed; every expected failure is handled
        inside — only genuinely unexpected exceptions escape to the
        caller's boundary handler.

        Args:
            connection: The accepted connection, adapted to the
                :class:`VoiceConnection` protocol.
            token: The validated Cognito token; its ``sub`` owns the
                session and its ``expires_at`` arms the mid-session
                expiry watchdog (Req 7.6).
        """
        runtime = _SessionRuntime(self, connection, token)
        await runtime.run()


class _SessionRuntime:
    """The complete lifecycle of one connection, from first frame to close.

    Instantiated per :meth:`VoiceSessionManager.run_session` call; holds
    every piece of per-session state (session snapshot, transcript
    accumulator, segmentation buffer, current stream) so concurrent
    sessions served by the same manager never share anything.
    """

    _session: VoiceSession
    """Current session snapshot; assigned before the lifecycle starts and
    re-assigned only after (or alongside) a Session_Store persist, so it
    never runs ahead of the durable record (Req 8.2)."""

    def __init__(
        self,
        manager: VoiceSessionManager,
        connection: VoiceConnection,
        token: ValidatedToken,
    ) -> None:
        """Initialize the runtime for one connection.

        Args:
            manager: The owning manager, supplying collaborators and
                policies.
            connection: The accepted connection being served.
            token: The validated token that authenticated the handshake.
        """
        self._connection = connection
        self._token = token
        self._stream_factory = manager._stream_factory
        self._store = manager._store
        self._tool_router = manager._tool_router
        self._protection = manager._protection
        self._drain = manager._drain
        self._system_prompt = manager._system_prompt
        self._rollover_seconds = manager._rollover_seconds
        self._watchdog_seconds = manager._watchdog_seconds
        self._idle_warning_seconds = manager._idle_warning_seconds
        self._idle_grace_seconds = manager._idle_grace_seconds
        self._retention_days = manager._retention_days
        self._transcript_retries = manager._transcript_retries
        self._clock = manager._clock
        self._monotonic = manager._monotonic
        self._sleep = manager._sleep
        self._logger = manager._logger
        self._id_factory = manager._id_factory
        self._accumulator = TranscriptAccumulator()
        self._audio_buffer = BoundedAudioBuffer()
        self._stream: BedrockStreamPort | None = None
        self._prompt_name = ""
        self._audio_content_name = ""
        self._text_input_count = 0
        # Monotonic instant of the last input event delivered to the live
        # stream (audio, typed text, or keepalive silence); drives the
        # keepalive that keeps the service-side input gap inside
        # BEDROCK_INPUT_GAP_LIMIT_SECONDS.
        self._last_input_at = 0.0
        # Monotonic instant of the last ENGINEER TURN — a recognized
        # spoken turn or a typed request. Deliberately NOT updated by
        # audio frames or keepalive silence: capture streams PCM
        # continuously, so _last_input_at above never goes stale on a
        # live connection and cannot tell a talking engineer from a
        # silent one. Set to "now" when pumping starts so a fresh session
        # never opens already idle.
        self._last_activity_at = 0.0
        self._idle_warned = False
        self._segmenting = False
        self._stream_opened_at = 0.0
        self._resume_pumping = asyncio.Event()
        self._resume_pumping.set()
        self._task_group: asyncio.TaskGroup | None = None
        self._resumed = False

    async def run(self) -> None:
        """Drive the connection end to end.

        Awaits the opening ``session.start`` frame, establishes the
        session (fresh or restored), and runs the full lifecycle inside a
        ``bind_session`` scope so every log entry carries the session
        identifier (Req 19.4). Connections refused before a session
        exists (bad first frame, rejected resume) are already answered
        and closed when this returns.
        """
        start = await self._receive_session_start()
        if start is None:
            return
        session = await self._establish_session(start)
        if session is None:
            return
        self._session = session
        with bind_session(session.session_id):
            await self._run_lifecycle()

    # ------------------------------------------------------------------
    # Session establishment
    # ------------------------------------------------------------------

    async def _receive_session_start(self) -> SessionStartFrame | None:
        """Await and validate the opening ``session.start`` frame.

        Returns:
            The parsed frame, or ``None`` when the client disconnected
            first or the first frame was not a valid ``session.start`` —
            in which case an ``internal`` error frame was sent and the
            connection closed.
        """
        try:
            message = await self._connection.receive()
        except ConnectionClosed:
            return None
        frame: object = None
        if isinstance(message, str):
            try:
                frame = parse_client_frame(message)
            except ProtocolError:
                self._logger.warning(
                    "Rejecting connection: malformed opening frame",
                    extra={"event": _EVENT_PROTOCOL_VIOLATION},
                )
        if isinstance(frame, SessionStartFrame):
            return frame
        await self._refuse_connection(START_EXPECTED_MESSAGE)
        return None

    async def _establish_session(
        self, start: SessionStartFrame
    ) -> VoiceSession | None:
        """Create a fresh session or restore a resumed one (Req 8.3, 8.6).

        Args:
            start: The parsed opening frame. When it carries a
                ``resumeSessionId``, the persisted snapshot and transcript
                are restored; otherwise a fresh session is created with
                the frame's execution or incident scoping.

        Returns:
            The session to serve, in ``CONNECTING`` state, or ``None``
            when a resume was rejected (the client was already answered
            and the connection closed).
        """
        if start.resume_session_id is not None:
            return await self._restore_session(start.resume_session_id)
        return create_session(
            session_id=self._id_factory(),
            engineer_id=self._token.sub,
            now=self._iso_now(),
            execution_id=start.execution_id,
            incident_context=start.incident_context,
        )

    async def _restore_session(self, resume_session_id: str) -> VoiceSession | None:
        """Restore a persisted session and its transcript for a reconnect.

        Reads the persisted snapshot and all transcript entries from the
        Session_Store (Req 8.3). A session that does not exist, is
        already terminal, or belongs to a different engineer is rejected
        with a ``session_not_found`` error frame (Req 8.6; the ownership
        check deliberately reports the same category, so a foreign
        session id leaks nothing). The restored snapshot re-enters the
        state machine at ``CONNECTING``; its identity, scoping, and
        segment count carry over, and the accumulator continues sequence
        numbering after the highest restored entry.

        Args:
            resume_session_id: The ``resumeSessionId`` presented by the
                client.

        Returns:
            The restored session in ``CONNECTING`` state, or ``None``
            when the resume was rejected or the store failed (the client
            was answered and the connection closed either way).
        """
        try:
            persisted = await self._store.get_session(resume_session_id)
        except SessionStoreError:
            self._logger.exception(
                "Resume rejected: session read failed",
                extra={
                    "event": _EVENT_PERSIST_FAILED,
                    "session_id": resume_session_id,
                },
            )
            await self._refuse_connection(INTERNAL_ERROR_MESSAGE)
            return None
        resumable = (
            persisted is not None
            and not persisted.state.is_terminal
            and persisted.engineer_id == self._token.sub
        )
        if persisted is None or not resumable:
            self._logger.warning(
                "Resume rejected: session not available",
                extra={
                    "event": _EVENT_RESUME_REJECTED,
                    "session_id": resume_session_id,
                },
            )
            await self._send_quietly(
                ErrorFrame(
                    category=ErrorCategory.SESSION_NOT_FOUND,
                    message=SESSION_NOT_FOUND_MESSAGE,
                    recoverable=False,
                )
            )
            await self._close_connection(CLOSE_CODE_ERROR, "session-not-found")
            return None
        try:
            entries = await self._store.get_transcript(resume_session_id)
        except SessionStoreError:
            self._logger.exception(
                "Resume rejected: transcript read failed",
                extra={
                    "event": _EVENT_PERSIST_FAILED,
                    "session_id": resume_session_id,
                },
            )
            await self._refuse_connection(INTERNAL_ERROR_MESSAGE)
            return None
        self._accumulator.restore(entries)
        self._resumed = True
        return replace(
            persisted,
            state=SessionState.CONNECTING,
            updated_at=self._iso_now(),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _run_lifecycle(self) -> None:
        """Run the session from first persist to terminal close.

        Persists the ``CONNECTING`` snapshot before confirming it
        (Req 8.2), reports the session to the protection manager
        (Req 10.3), registers the drain terminator (Req 10.8), and
        guarantees — via ``finally`` — that the drain registration, the
        protection count, and the current stream are released on every
        exit path, expected or not.
        """
        session_id = self._session.session_id
        if not await self._persist_initial_state():
            return
        self._logger.info(
            "Voice session started",
            extra={
                "event": _EVENT_SESSION_STARTED,
                "engineer_id": self._session.engineer_id,
                "resumed": self._resumed,
                "execution_id": self._session.execution_id,
            },
        )
        await self._protection.session_started()
        self._drain.register(session_id, self._terminate_for_drain)
        try:
            await self._serve()
        finally:
            self._drain.unregister(session_id)
            await self._protection.session_ended()
            await self._close_stream_quietly()

    async def _persist_initial_state(self) -> bool:
        """Persist the ``CONNECTING`` snapshot, then confirm it (Req 8.2).

        Returns:
            ``True`` when the snapshot is durable and the ``connecting``
            state frame was offered to the client (Req 9.5); ``False``
            when the write failed — the connection was then refused with
            an ``internal`` error frame and closed, and no session
            lifecycle starts (Req 8.5).
        """
        try:
            await self._store.put_session(self._session, ttl=self._ttl())
        except SessionStoreError:
            self._logger.exception(
                "Session could not be persisted at creation",
                extra={"event": _EVENT_PERSIST_FAILED},
            )
            await self._refuse_connection(INTERNAL_ERROR_MESSAGE)
            return False
        await self._send_quietly(
            SessionStateFrame(
                session_id=self._session.session_id, state=self._session.state
            )
        )
        return True

    async def _serve(self) -> None:
        """Open the first Bedrock_Stream and pump until the session ends.

        A stream that cannot be opened ends the session with the
        ``bedrock_unavailable`` error frame before the close (Req 1.6).
        Once live, the four pump tasks run until one of them ends the
        session, after which the terminal sequence runs (Req 2.5).
        """
        try:
            await self._open_new_stream()
        except BedrockStreamError:
            self._logger.exception(
                "Bedrock stream could not be opened",
                extra={"event": _EVENT_STREAM_OPEN_FAILED},
            )
            await self._fail_stream_open()
            return
        try:
            await self._transition_and_report(SessionEvent.STREAM_OPENED)
        except SessionStoreError:
            self._logger.exception(
                "Live state could not be persisted",
                extra={"event": _EVENT_PERSIST_FAILED},
            )
            await self._refuse_connection(INTERNAL_ERROR_MESSAGE)
            return
        except ConnectionClosed:
            await self._finalize(_EndReason.CLIENT_DISCONNECT)
            return
        reason = await self._pump_until_end()
        await self._finalize(reason)

    async def _fail_stream_open(self) -> None:
        """End a session whose Bedrock_Stream could not be opened (Req 1.6).

        Persists the ``ERROR`` state and reports it, sends the
        ``bedrock_unavailable`` error frame, and closes the WebSocket —
        no further engineer audio is processed because the pumps never
        started.
        """
        await self._report_terminal(SessionEvent.STREAM_OPEN_FAILED)
        await self._send_quietly(
            ErrorFrame(
                category=ErrorCategory.BEDROCK_UNAVAILABLE,
                message=BEDROCK_UNAVAILABLE_MESSAGE,
                recoverable=False,
            )
        )
        await self._close_connection(CLOSE_CODE_ERROR, "bedrock-unavailable")

    async def _pump_until_end(self) -> _EndReason:
        """Run the six pump tasks until one of them ends the session.

        The task group holds the inbound pump, the outbound pump, the
        segmentation timer, the token-expiry watchdog, the audio
        keepalive, and the idle watchdog (plus any tool tasks spawned
        while pumping). The first ``_SessionEnd`` raised cancels the
        siblings and its reason is returned; unexpected exceptions
        propagate to the boundary handler after the group unwinds.

        Returns:
            The reason the session stopped pumping.
        """
        # Start the idle interval at "now": the field is 0.0 until here,
        # which would read as an unbounded idle time and warn on the
        # first poll of a brand-new session.
        self._last_activity_at = self._monotonic()
        self._idle_warned = False
        reason: _EndReason | None = None
        try:
            async with asyncio.TaskGroup() as group:
                self._task_group = group
                group.create_task(
                    self._guard(self._inbound_pump), name="vsm-inbound"
                )
                group.create_task(
                    self._guard(self._outbound_pump), name="vsm-outbound"
                )
                group.create_task(
                    self._guard(self._segmentation_timer), name="vsm-segmentation"
                )
                group.create_task(
                    self._guard(self._token_watchdog), name="vsm-token-watchdog"
                )
                group.create_task(
                    self._guard(self._audio_keepalive), name="vsm-keepalive"
                )
                group.create_task(
                    self._guard(self._idle_watchdog), name="vsm-idle-watchdog"
                )
        except* _SessionEnd as ends:
            reason = _first_reason(ends)
        finally:
            self._task_group = None
        return reason if reason is not None else _EndReason.INTERNAL

    async def _guard(self, pump: Callable[[], Awaitable[None]]) -> None:
        """Run one pump task, converting a lost connection into an end signal.

        Args:
            pump: The pump coroutine function to run.

        Raises:
            _SessionEnd: With ``CLIENT_DISCONNECT`` when the pump hit a
                closed connection; pumps' own end signals pass through
                unchanged.
        """
        try:
            await pump()
        except ConnectionClosed as error:
            raise _SessionEnd(_EndReason.CLIENT_DISCONNECT) from error

    # ------------------------------------------------------------------
    # Pump tasks
    # ------------------------------------------------------------------

    async def _inbound_pump(self) -> None:
        """Relay client frames: audio to the stream, control frames to actions.

        Binary frames are forwarded to the live stream immediately on
        receipt (Req 1.2) or buffered while a rollover is in progress
        (Req 2.4); audio received after the token expiry is dropped
        (Req 7.6). Text frames are parsed as control frames; a
        ``session.end`` ends the session gracefully (Req 2.5) and a
        malformed frame is logged and ignored.

        Raises:
            _SessionEnd: When the client disconnected, requested the end,
                or the live stream failed on a send.
        """
        while True:
            try:
                message = await self._connection.receive()
            except ConnectionClosed as error:
                raise _SessionEnd(_EndReason.CLIENT_DISCONNECT) from error
            if isinstance(message, bytes):
                await self._handle_inbound_audio(message)
            else:
                await self._handle_inbound_text(message)

    async def _audio_keepalive(self) -> None:
        """Hold the Bedrock_Stream open through gaps in client input.

        Nova Sonic closes a stream that receives neither audio bytes nor
        interactive content for :data:`BEDROCK_INPUT_GAP_LIMIT_SECONDS`
        (observed in production: a backgrounded browser tab throttles the
        capture AudioWorklet, the PCM flow stops, and the service ends the
        stream with a ``ValidationException`` that surfaced to the engineer
        as an unexplained internal error). This task sends one silent
        frame after :data:`KEEPALIVE_IDLE_SECONDS` of input silence, which
        resets the service-side timer without adding anything audible to
        the conversation.

        Silence is skipped while a segmentation rollover is in progress:
        the replacement stream starts a fresh timer anyway, and its audio
        content block is not open yet.
        """
        while True:
            await self._sleep(KEEPALIVE_POLL_SECONDS)
            if self._segmenting:
                continue
            idle_for = self._monotonic() - self._last_input_at
            if idle_for < KEEPALIVE_IDLE_SECONDS:
                continue
            self._logger.info(
                "Client input idle; sending silence to hold the stream open",
                extra={"event": _EVENT_KEEPALIVE, "idle_seconds": round(idle_for, 1)},
            )
            try:
                await self._current_stream().send_audio(
                    self._prompt_name,
                    self._audio_content_name,
                    bytes(_KEEPALIVE_SILENCE_BYTES),
                )
            except BedrockStreamError:
                # A failing keepalive is never itself a reason to end the
                # session: the pumps own that decision, and a rollover
                # racing this send is benign.
                self._logger.warning(
                    "Keepalive silence send failed; leaving the session to the pumps",
                    extra={"event": _EVENT_KEEPALIVE},
                )
                continue
            self._last_input_at = self._monotonic()

    async def _handle_inbound_audio(self, pcm: bytes) -> None:
        """Forward or buffer one client audio frame.

        Args:
            pcm: Raw 16 kHz 16-bit mono PCM bytes from the client
                (Req 1.1).

        Raises:
            _SessionEnd: With ``STREAM_FAILED`` when the live stream
                rejected the send outside a rollover.
        """
        if self._clock() >= self._token.expires_at:
            # Req 7.6: audio received after the expiry instant is never
            # processed; the watchdog is ending the session concurrently.
            return
        if self._segmenting:
            self._buffer_audio(pcm)
            return
        self._last_input_at = self._monotonic()
        try:
            await self._current_stream().send_audio(
                self._prompt_name, self._audio_content_name, pcm
            )
        except BedrockStreamError as error:
            if self._segmenting:
                # The send raced the rollover's stream teardown; the frame
                # joins the buffer in order (nothing was buffered before
                # the flag flipped within this event-loop step).
                self._buffer_audio(pcm)
            else:
                self._logger.exception(
                    "Audio send failed on the live stream",
                    extra={"event": _EVENT_STREAM_FAILED},
                )
                raise _SessionEnd(_EndReason.STREAM_FAILED) from error

    def _buffer_audio(self, pcm: bytes) -> None:
        """Buffer one audio frame during segmentation, logging drops (Req 2.4).

        Args:
            pcm: The audio frame to buffer; dropped whole (and counted)
                when it would exceed the 30-second capacity.
        """
        if not self._audio_buffer.append(pcm):
            self._logger.warning(
                "Segmentation buffer full; dropping newest audio frame",
                extra={
                    "event": _EVENT_AUDIO_DROPPED,
                    "dropped_frames": self._audio_buffer.dropped_frames,
                    "dropped_bytes": self._audio_buffer.dropped_bytes,
                },
            )

    async def _handle_inbound_text(self, message: str) -> None:
        """Handle one client control frame received mid-session.

        A typed request arriving after the token expiry instant is
        dropped, mirroring the audio path (Req 7.6); ``session.end`` is
        still honored so the engineer can always end the session.

        Args:
            message: Raw text payload of the frame.

        Raises:
            _SessionEnd: With ``CLIENT_END`` when the frame is
                ``session.end`` (Req 2.5), or with ``STREAM_FAILED`` when
                a typed request cannot be delivered to the live stream.
        """
        try:
            frame = parse_client_frame(message)
        except ProtocolError:
            self._logger.warning(
                "Ignoring malformed control frame on a live session",
                extra={"event": _EVENT_PROTOCOL_VIOLATION},
            )
            return
        if isinstance(frame, SessionEndFrame):
            raise _SessionEnd(_EndReason.CLIENT_END)
        if isinstance(frame, TextInputFrame):
            if self._clock() >= self._token.expires_at:
                # Req 7.6 applies to a typed request exactly as it does to
                # audio: nothing the engineer sends after the expiry
                # instant is processed. ``session.end`` above is
                # deliberately still honored — ending a session is not
                # "processing a request", and the engineer should always
                # be able to hang up.
                return
            await self._handle_text_input(frame.text)
            return
        self._logger.warning(
            "Ignoring session.start on an already-started session",
            extra={"event": _EVENT_PROTOCOL_VIOLATION},
        )

    async def _handle_text_input(self, text: str) -> None:
        """Route one typed engineer request into the live conversation.

        A typed request joins the same conversation as a spoken one: it is
        persisted and echoed as a ``transcript`` frame (so it appears in
        the engineer's own transcript pane and survives reconnect), then
        forwarded to the live stream as a USER text block, which
        Nova_Sonic answers by voice and may route through the
        ``ask_devops_agent`` tool exactly as for speech.

        Persist-then-forward, matching the model-output path (Req 8.2):
        the entry joins the accumulator first, so a request typed during a
        segmentation rollover is replayed into the replacement stream as
        history even when the live send fails.

        Args:
            text: The typed request text, guaranteed non-blank by frame
                parsing.

        Raises:
            _SessionEnd: With ``STREAM_FAILED`` when the live stream
                rejects the text outside a rollover.
            ConnectionClosed: If the client vanished while the echo frame
                was being sent (converted to a graceful end by the pump
                guard).
        """
        # A typed request is an engineer turn: clear the idle interval
        # before any await, so a slow persist cannot let the idle
        # watchdog end a session the engineer is actively using.
        self._mark_engineer_turn()
        entry = self._accumulator.append(Role.USER, text, self._iso_now())
        try:
            await retry_async(
                partial(
                    self._store.append_transcript,
                    self._session.session_id,
                    entry,
                    ttl=self._ttl(),
                ),
                retryable=SessionStoreError,
                retries=self._transcript_retries,
                sleep=self._sleep,
                logger=self._logger,
                name="session_store.append_transcript",
            )
        except SessionStoreError:
            # Exhaustion is already logged with the session id (Req 2.7).
            # The entry stays in the accumulator, so the request still
            # reaches the model; only its durability was lost.
            self._logger.warning(
                "Typed request was not persisted; forwarding it anyway",
                extra={"event": _EVENT_TEXT_INPUT},
            )
        else:
            await self._send_frame(
                TranscriptFrame(
                    role=Role.USER, text=text, timestamp=entry.timestamp
                )
            )

        self._text_input_count += 1
        content_name = f"{_TEXT_INPUT_CONTENT_NAME_PREFIX}{self._text_input_count}"
        events = build_text_input_block(
            prompt_name=self._prompt_name,
            content_name=content_name,
            role=Role.USER.value,
            text=text,
        )
        self._logger.info(
            "Forwarding typed engineer request to the live stream",
            extra={"event": _EVENT_TEXT_INPUT, "characters": len(text)},
        )
        # Interactive content resets the service-side input-gap timer too.
        self._last_input_at = self._monotonic()
        try:
            for event in events:
                await self._current_stream().send_event(event)
        except BedrockStreamError as error:
            if self._segmenting:
                # The rollover replays the accumulator as history, so the
                # typed request reaches the replacement stream anyway.
                self._logger.warning(
                    "Typed request raced a segmentation rollover; it will be "
                    "replayed as history",
                    extra={"event": _EVENT_TEXT_INPUT},
                )
                return
            self._logger.exception(
                "Typed request send failed on the live stream",
                extra={"event": _EVENT_STREAM_FAILED},
            )
            raise _SessionEnd(_EndReason.STREAM_FAILED) from error

    async def _outbound_pump(self) -> None:
        """Relay stream output events to the client, across stream swaps.

        Iterates the current stream's output; when a segmentation
        rollover replaces the stream, the old iteration drains and ends,
        the pump waits for the swap to complete, and iteration resumes on
        the replacement stream. The live stream ending or failing outside
        a rollover ends the session.

        Raises:
            _SessionEnd: With ``STREAM_FAILED`` when the live stream
                failed mid-iteration, or ``STREAM_ENDED`` when it
                completed on its own.
        """
        while True:
            stream = self._current_stream()
            try:
                async for event in stream.receive():
                    await self._handle_output_event(event)
            except BedrockStreamError as error:
                if stream is self._stream and not self._segmenting:
                    self._logger.exception(
                        "Live stream failed mid-session",
                        extra={"event": _EVENT_STREAM_FAILED},
                    )
                    raise _SessionEnd(_EndReason.STREAM_FAILED) from error
                # A replaced stream failing while it drains is part of the
                # rollover; fall through and wait for the swap.
            if stream is self._stream and not self._segmenting:
                raise _SessionEnd(_EndReason.STREAM_ENDED)
            await self._resume_pumping.wait()

    async def _handle_output_event(self, event: Mapping[str, object]) -> None:
        """Dispatch one decoded stream output event.

        ``textOutput`` becomes a persisted transcript entry and a
        ``transcript`` frame (Req 1.4) — unless it carries Nova Sonic's
        JSON interruption marker, which becomes an ``interrupted`` frame
        instead of a transcript line; ``audioOutput`` becomes a binary
        frame (Req 1.3); ``toolUse`` spawns a tool task (Req 3.4); a
        ``contentEnd`` whose ``stopReason`` is ``INTERRUPTED`` reports the
        barge-in as an ``interrupted`` frame; every other event
        (``completionStart``, ``contentStart``, other ``contentEnd``
        variants, ``completionEnd``, and forward-compatible names) needs
        no action here.

        Args:
            event: One decoded output event, discriminated by ``"type"``.
        """
        event_type = event.get("type")
        if event_type == _TEXT_OUTPUT_EVENT:
            if _is_interruption_marker(event.get("content")):
                await self._notify_interrupted()
                return
            await self._relay_transcript(event)
        elif event_type == _AUDIO_OUTPUT_EVENT:
            await self._relay_audio(event)
        elif event_type == _TOOL_USE_EVENT:
            self._spawn_tool_task(event)
        elif (
            event_type == _CONTENT_END_EVENT
            and event.get("stopReason") == _INTERRUPTED_STOP_REASON
        ):
            await self._notify_interrupted()

    async def _notify_interrupted(self) -> None:
        """Relay one barge-in notice to the client (``interrupted`` frame).

        Nova Sonic reported that the engineer spoke over the assistant's
        response; the client reacts by flushing its playback queue so the
        cancelled speech stops immediately. The already-generated audio
        for the cancelled response is not replayed — the model abandons
        it — so no server-side buffer needs clearing.

        Raises:
            ConnectionClosed: If the client vanished while the frame was
                being sent (converted to a graceful end by the pump
                guard).
        """
        self._logger.info(
            "Engineer interrupted the response; instructing playback flush",
            extra={"event": _EVENT_INTERRUPTED},
        )
        await self._send_frame(InterruptedFrame())

    async def _relay_transcript(self, event: Mapping[str, object]) -> None:
        """Accumulate, persist, then relay one transcript line (Req 1.4, 2.7).

        The entry joins the accumulator (feeding segmentation replay and
        reconnect restoration) and is persisted under the bounded retry
        policy; the ``transcript`` frame is sent only after the write is
        durable (Req 8.2). When every retry fails, the exhaustion is
        already logged with the session identifier (Req 2.7) and the
        frame is withheld.

        Args:
            event: The ``textOutput`` event; its ``role`` selects the
                speaker (``USER`` for ASR text, ``ASSISTANT`` otherwise)
                and its ``content`` carries the text. Events without
                usable text are ignored.

        Raises:
            ConnectionClosed: If the client vanished while the frame was
                being sent (converted to a graceful end by the pump
                guard).
        """
        content = event.get("content")
        if not isinstance(content, str) or not content:
            return
        role_value = event.get("role")
        role = (
            Role.USER
            if isinstance(role_value, str) and role_value.upper() == Role.USER.value
            else Role.ASSISTANT
        )
        if role is Role.USER:
            # Recognized speech is the engineer's own turn, and the only
            # signal that distinguishes a talking engineer from an open
            # microphone in a silent room.
            self._mark_engineer_turn()
        entry = self._accumulator.append(role, content, self._iso_now())
        try:
            await retry_async(
                partial(
                    self._store.append_transcript,
                    self._session.session_id,
                    entry,
                    ttl=self._ttl(),
                ),
                retryable=SessionStoreError,
                retries=self._transcript_retries,
                sleep=self._sleep,
                logger=self._logger,
                name="session_store.append_transcript",
            )
        except SessionStoreError:
            # Exhaustion was logged with the session id by the retry
            # helper (Req 2.7); the frame is withheld because the entry
            # never became durable (Req 8.2). The session continues.
            return
        await self._send_frame(
            TranscriptFrame(role=role, text=content, timestamp=entry.timestamp)
        )

    async def _relay_audio(self, event: Mapping[str, object]) -> None:
        """Relay one ``audioOutput`` event as a binary frame (Req 1.3).

        Args:
            event: The ``audioOutput`` event; its ``content`` carries the
                base64-encoded 24 kHz PCM. Events with missing or invalid
                content are logged and dropped.

        Raises:
            ConnectionClosed: If the client vanished while the frame was
                being sent (converted to a graceful end by the pump
                guard).
        """
        content = event.get("content")
        if not isinstance(content, str) or not content:
            return
        try:
            pcm = base64.b64decode(content)
        except ValueError:
            self._logger.warning(
                "Discarding audioOutput event with invalid base64 content",
                extra={"event": _EVENT_PROTOCOL_VIOLATION},
            )
            return
        await self._connection.send_bytes(pcm)

    def _spawn_tool_task(self, event: Mapping[str, object]) -> None:
        """Spawn one tool task for a ``toolUse`` event (Req 3.4).

        The task runs in the session's task group so the outbound pump
        keeps relaying audio and transcripts while the DevOps Agent
        works. The stream and prompt name are captured now: the result
        belongs to the stream that raised the ``toolUse`` and is dropped
        if that stream is gone by the time the result is ready.

        Args:
            event: The ``toolUse`` event carrying ``toolUseId``,
                ``toolName``, and the tool input (``content``). Events
                without a usable ``toolUseId``, or naming a tool other
                than ``ask_devops_agent``, are logged and ignored.
        """
        tool_use_id = event.get("toolUseId")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            self._logger.warning(
                "Discarding toolUse event without a toolUseId",
                extra={"event": _EVENT_PROTOCOL_VIOLATION},
            )
            return
        tool_name = event.get("toolName")
        if tool_name != ASK_DEVOPS_AGENT_TOOL_NAME:
            self._logger.warning(
                "Discarding toolUse event for an unregistered tool",
                extra={
                    "event": _EVENT_PROTOCOL_VIOLATION,
                    "tool_name": tool_name,
                },
            )
            return
        raw_input = event.get("content")
        tool_input: str | Mapping[str, object]
        if isinstance(raw_input, (Mapping, str)):
            tool_input = raw_input
        else:
            tool_input = ""
        group = self._task_group
        if group is None:
            return
        group.create_task(
            self._run_tool(
                self._current_stream(), self._prompt_name, tool_use_id, tool_input
            ),
            name=f"vsm-tool-{tool_use_id}",
        )

    async def _run_tool(
        self,
        stream: BedrockStreamPort,
        prompt_name: str,
        tool_use_id: str,
        tool_input: str | Mapping[str, object],
    ) -> None:
        """Route one tool invocation and return its result into the stream.

        The tool router converts every expected failure — guardrail
        block, agent error, timeout, store error — into an error tool
        result, so this task raises nothing for those (Req 3.7, 4.3). A
        result whose originating stream was replaced by a rollover before
        delivery is logged and dropped: the replacement stream has no
        knowledge of the pending ``toolUseId``.

        Args:
            stream: The stream the ``toolUse`` event arrived on.
            prompt_name: That stream's prompt identifier.
            tool_use_id: The ``toolUseId`` the result answers.
            tool_input: The tool input as delivered by the stream.
        """
        result = await self._tool_router.handle_tool_use(
            session_id=self._session.session_id,
            engineer_id=self._session.engineer_id,
            execution_id=self._session.execution_id,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
        )
        try:
            await stream.send_tool_result(
                prompt_name, result.tool_use_id, result.content
            )
        except BedrockStreamError:
            self._logger.warning(
                "Tool result could not be delivered; its stream is gone",
                extra={
                    "event": _EVENT_TOOL_RESULT_DROPPED,
                    "tool_use_id": tool_use_id,
                },
            )

    async def _segmentation_timer(self) -> None:
        """Trigger a segmentation rollover at the configured stream age.

        Sleeps until the current stream's age reaches the rollover
        threshold (Req 2.2), performs the rollover, and repeats for the
        replacement stream, so a session spans any number of consecutive
        Bedrock_Streams.

        Raises:
            _SessionEnd: With ``SEGMENTATION_FAILED`` when a rollover
                failed (raised by the rollover itself), or ``INTERNAL``
                when the ``SEGMENTING`` state could not be persisted.
        """
        try:
            while True:
                age = self._monotonic() - self._stream_opened_at
                remaining = seconds_until_rollover(age, self._rollover_seconds)
                if remaining > 0:
                    await self._sleep(remaining)
                    continue
                await self._perform_segmentation()
        except SessionStoreError as error:
            self._logger.exception(
                "Segmentation state could not be persisted",
                extra={"event": _EVENT_PERSIST_FAILED},
            )
            raise _SessionEnd(_EndReason.INTERNAL) from error

    async def _perform_segmentation(self) -> None:
        """Roll the session onto a fresh Bedrock_Stream (Req 2.2-2.4, 2.6).

        Sequence: persist and report ``SEGMENTING`` (Req 8.2, 9.5); start
        buffering inbound audio (Req 2.4); then — inside the watchdog
        budget (Req 2.6) — close the expiring stream, open the
        replacement with the full context replay (system prompt, tool
        configuration, complete history in original order, Req 2.3),
        reopen the interactive audio block, and flush the buffered audio
        FIFO in arrival order (Req 2.4); finally persist and report the
        return to ``LIVE`` and release the outbound pump onto the new
        stream. Transcript entries are already persisted incrementally,
        so the partial transcript required on failure is durable before
        the failure is reported (Req 2.6).

        Raises:
            SessionStoreError: If the ``SEGMENTING`` state could not be
                persisted (the stream is still live; the caller ends the
                session as internal).
            ConnectionClosed: If a state frame hit a closed connection.
            _SessionEnd: With ``SEGMENTATION_FAILED`` when the rollover
                failed or overran its watchdog (Req 2.6).
        """
        self._logger.info(
            "Segmentation due; rolling onto a fresh stream",
            extra={
                "event": _EVENT_SEGMENTATION_STARTED,
                "segment_count": self._session.segment_count,
            },
        )
        await self._transition_and_report(SessionEvent.SEGMENTATION_DUE)
        self._segmenting = True
        self._resume_pumping.clear()
        old_stream = self._current_stream()
        try:
            async with asyncio.timeout(self._watchdog_seconds):
                await old_stream.close()
                await self._open_new_stream()
                await self._drain_audio_buffer()
        except (TimeoutError, BedrockStreamError) as error:
            self._logger.exception(
                "Segmentation rollover failed",
                extra={
                    "event": _EVENT_SEGMENTATION_FAILED,
                    "session_id": self._session.session_id,
                },
            )
            raise _SessionEnd(_EndReason.SEGMENTATION_FAILED) from error
        try:
            await self._transition_and_report(SessionEvent.ROLLOVER_COMPLETE)
        except SessionStoreError as error:
            self._logger.exception(
                "Rollover completion could not be persisted",
                extra={
                    "event": _EVENT_SEGMENTATION_FAILED,
                    "session_id": self._session.session_id,
                },
            )
            raise _SessionEnd(_EndReason.SEGMENTATION_FAILED) from error
        self._resume_pumping.set()
        self._logger.info(
            "Segmentation rollover complete",
            extra={
                "event": _EVENT_SEGMENTATION_COMPLETED,
                "segment_count": self._session.segment_count,
            },
        )

    async def _drain_audio_buffer(self) -> None:
        """Flush buffered audio into the new stream, then resume direct sends.

        Delivers every buffered frame in arrival order (Req 2.4). Loops
        because frames may keep arriving while earlier ones are flushed;
        the ``_segmenting`` flag is cleared synchronously with the final
        emptiness check — no ``await`` between them — so no frame can
        slip into the buffer after the last flush and be stranded.

        Raises:
            BedrockStreamError: If the new stream rejects a buffered
                frame (fails the rollover, Req 2.6).
        """
        while True:
            frames = self._audio_buffer.flush()
            if not frames:
                self._segmenting = False
                return
            for frame in frames:
                await self._current_stream().send_audio(
                    self._prompt_name, self._audio_content_name, frame
                )

    async def _token_watchdog(self) -> None:
        """End the session when the Cognito token expires (Req 7.6).

        Sleeps until the validated token's ``expires_at`` passes, then
        ends the session; the terminal sequence sends the
        ``auth_expired`` error frame before closing, and the inbound pump
        independently drops any audio received after the expiry instant.

        Raises:
            _SessionEnd: With ``AUTH_EXPIRED`` once the expiry passed.
        """
        while True:
            remaining = self._token.expires_at - self._clock()
            if remaining <= 0:
                break
            await self._sleep(remaining)
        self._logger.warning(
            "Token expired mid-session; ending the session",
            extra={"event": _EVENT_AUTH_EXPIRED},
        )
        raise _SessionEnd(_EndReason.AUTH_EXPIRED)

    async def _idle_watchdog(self) -> None:
        """Warn a quiet engineer, then end the session if they stay quiet.

        Idleness is measured between engineer turns — a recognized spoken
        turn or a typed request — never between audio frames, because
        capture streams PCM continuously and a silent engineer looks
        identical to a talking one at that level (see
        :data:`DEFAULT_IDLE_WARNING_SECONDS`).

        After ``idle_warning_seconds`` of silence an ``idle_warning``
        notice goes out once, and the session keeps running. If the
        silence reaches ``idle_warning_seconds + idle_grace_seconds`` the
        session ends; the terminal sequence sends the ``idle_timeout``
        frame and closes normally. A turn at any point clears the
        interval and re-arms the warning, so an engineer who comes back
        keeps their session and can be warned again later.

        Raises:
            _SessionEnd: With ``IDLE_TIMEOUT`` once the warning's grace
                period elapsed with no engineer turn.
            ConnectionClosed: If the client vanished while the notice was
                being sent (converted to a graceful end by the pump
                guard).
        """
        deadline = self._idle_warning_seconds + self._idle_grace_seconds
        while True:
            await self._sleep(IDLE_POLL_SECONDS)
            idle_for = self._monotonic() - self._last_activity_at
            if idle_for >= deadline:
                self._logger.info(
                    "Engineer idle past the warning grace period; ending the session",
                    extra={
                        "event": _EVENT_IDLE_TIMEOUT,
                        "idle_seconds": round(idle_for, 1),
                    },
                )
                raise _SessionEnd(_EndReason.IDLE_TIMEOUT)
            if idle_for < self._idle_warning_seconds:
                # A turn landed: re-arm so a later quiet spell warns again.
                self._idle_warned = False
                continue
            if self._idle_warned:
                continue
            self._idle_warned = True
            self._logger.info(
                "Engineer idle; warning before the session ends",
                extra={
                    "event": _EVENT_IDLE_WARNING,
                    "idle_seconds": round(idle_for, 1),
                },
            )
            await self._send_frame(
                ErrorFrame(
                    category=ErrorCategory.IDLE_WARNING,
                    message=IDLE_WARNING_MESSAGE,
                    recoverable=True,
                )
            )

    def _mark_engineer_turn(self) -> None:
        """Record an engineer turn, clearing the idle interval.

        Called for a recognized spoken turn and for a typed request —
        the two things that mean the engineer is still there. Resetting
        :attr:`_idle_warned` here as well as in the watchdog keeps a
        warned-then-active session from ending on the next poll.
        """
        self._last_activity_at = self._monotonic()
        self._idle_warned = False

    # ------------------------------------------------------------------
    # Stream handling
    # ------------------------------------------------------------------

    def _current_stream(self) -> BedrockStreamPort:
        """Return the current Bedrock_Stream port.

        Returns:
            The stream serving the session right now.

        Raises:
            BedrockStreamError: If no stream has been opened yet — a
                programming error, since every caller runs after the
                first open.
        """
        stream = self._stream
        if stream is None:
            raise BedrockStreamError(_NO_STREAM_DETAIL)
        return stream

    async def _open_new_stream(self) -> None:
        """Open a fresh Bedrock_Stream with the full context replay.

        Builds the replay sequence — system prompt (with incident context
        injected for incident-scoped sessions, Req 5.8), the
        ``ask_devops_agent`` tool configuration (Req 3.1), and the
        accumulated history in original order (Req 2.3, 8.3) — opens a
        fresh port instance with it, then opens the interactive audio
        content block. On success the runtime's stream, prompt name,
        audio content name, and stream-age origin are swapped to the new
        stream.

        Raises:
            StreamOpenError: If the stream cannot be established
                (Req 1.6).
            BedrockStreamError: If the audio content block cannot be
                opened on the established stream.
        """
        prompt_name = self._id_factory()
        audio_content_name = f"audio-{self._id_factory()}"
        stream = self._stream_factory()
        events = build_replay_sequence(
            prompt_name=prompt_name,
            system_prompt=self._effective_system_prompt(),
            tool_configuration=ASK_DEVOPS_AGENT_TOOL_SPEC,
            history=self._accumulator.entries(),
        )
        await stream.open(events)
        await stream.send_event(
            {
                "type": "contentStart",
                "promptName": prompt_name,
                "contentName": audio_content_name,
                "contentType": "AUDIO",
                "role": Role.USER.value,
            }
        )
        self._stream = stream
        self._prompt_name = prompt_name
        self._audio_content_name = audio_content_name
        # A fresh stream starts a fresh service-side input-gap timer.
        self._last_input_at = self._monotonic()
        self._stream_opened_at = self._monotonic()

    def _effective_system_prompt(self) -> str:
        """Return the system prompt, with incident context when present.

        Returns:
            The configured system prompt; for a session opened from an
            incident notification without an executionId, the incident
            summary and severity are appended so Nova_Sonic starts with
            the incident context (Req 5.8).
        """
        context = self._session.incident_context
        if context is None:
            return self._system_prompt
        return (
            f"{self._system_prompt}\n\n"
            "Incident context for this session:\n"
            f"Summary: {context.summary}\n"
            f"Severity: {context.severity}"
        )

    async def _close_stream_quietly(self) -> None:
        """Tear down the current stream, tolerating teardown failures.

        The adapter's close performs the graceful ``promptEnd`` →
        ``sessionEnd`` handshake (Req 2.5) and is idempotent, so this is
        safe on every exit path; a failing teardown is logged and never
        masks the session outcome.
        """
        stream = self._stream
        if stream is None:
            return
        try:
            await stream.close()
        except BedrockStreamError:
            self._logger.warning(
                "Bedrock stream teardown failed",
                extra={"event": _EVENT_STREAM_FAILED},
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # State reporting and terminal sequence
    # ------------------------------------------------------------------

    async def _transition_and_report(self, event: SessionEvent) -> None:
        """Advance the state machine: persist the new snapshot, then report it.

        Persist-before-confirm (Req 8.2, design Property 11): the new
        snapshot is written to the Session_Store first, adopted locally
        only after the write returned, and the ``session.state`` frame —
        one per transition (Req 9.5) — is sent last.

        Args:
            event: The state-machine event to raise.

        Raises:
            SessionStoreError: If the persist failed; the local snapshot
                is not adopted, so state never runs ahead of the store
                (Req 8.5).
            ConnectionClosed: If the state frame hit a closed connection;
                the snapshot is already durable and adopted.
            IllegalTransitionError: If the event is not legal in the
                current state — a programming error, as call sites guard
                their states.
        """
        next_session = transition(self._session, event, now=self._iso_now())
        await self._store.put_session(next_session, ttl=self._ttl())
        self._session = next_session
        await self._send_frame(
            SessionStateFrame(
                session_id=next_session.session_id, state=next_session.state
            )
        )

    async def _report_terminal(self, event: SessionEvent) -> None:
        """Best-effort terminal transition: persist, then confirm (Req 8.2).

        Terminal paths must always reach the close, so persistence
        failures are logged (Req 8.5) instead of raised; when the persist
        failed, the state frame is withheld — an unconfirmed state change
        is never reported (Req 8.2) — and the close itself tells the
        client the session is over.

        Args:
            event: The terminal state-machine event to raise.
        """
        next_session = transition(self._session, event, now=self._iso_now())
        self._session = next_session
        try:
            await self._store.put_session(next_session, ttl=self._ttl())
        except SessionStoreError:
            self._logger.exception(
                "Terminal session state could not be persisted",
                extra={"event": _EVENT_PERSIST_FAILED},
            )
            return
        await self._send_quietly(
            SessionStateFrame(
                session_id=next_session.session_id, state=next_session.state
            )
        )

    async def _finalize(self, reason: _EndReason) -> None:
        """Run the terminal sequence for a session that stopped pumping.

        Order: tear the stream down (``promptEnd``/``sessionEnd``,
        Req 2.5); persist the terminal state and confirm it (Req 8.2,
        9.5) — the final transcript is already durable through
        incremental persistence (Req 2.5, 2.6); send the error frame the
        reason owes (``auth_expired`` Req 7.6, ``segmentation_failed``
        Req 2.6, or ``internal``) after the persist so no report precedes
        durability; close the WebSocket with a normal code for graceful
        ends and 1011 otherwise.

        Args:
            reason: Why the session stopped pumping.
        """
        await self._close_stream_quietly()
        event = _terminal_event_for(self._session.state)
        if event is not None:
            await self._report_terminal(event)
        error_frame = _error_frame_for(reason)
        if error_frame is not None:
            await self._send_quietly(error_frame)
        code = CLOSE_CODE_NORMAL if reason.is_graceful else CLOSE_CODE_ERROR
        await self._close_connection(code, reason.value)
        self._logger.info(
            "Voice session ended",
            extra={
                "event": _EVENT_SESSION_ENDED,
                "reason": reason.value,
                "state": self._session.state.value,
                "session_id": self._session.session_id,
            },
        )

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def _terminate_for_drain(self) -> None:
        """Notify and close a session outliving the drain period (Req 10.8).

        Registered with the drain manager; sends the
        ``session.terminating`` notice with reason ``drain_timeout`` and
        closes the WebSocket, which unwinds the pumps through their
        ``ConnectionClosed`` handling and ends the session gracefully.
        """
        await self._send_quietly(
            SessionTerminatingFrame(reason=DRAIN_TERMINATING_REASON)
        )
        await self._close_connection(CLOSE_CODE_GOING_AWAY, DRAIN_TERMINATING_REASON)

    async def _send_frame(self, frame: ServerFrame) -> None:
        """Serialize and send one server control frame.

        Args:
            frame: The typed server frame to send.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        await self._connection.send_text(serialize_server_frame(frame))

    async def _send_quietly(self, frame: ServerFrame) -> None:
        """Send one server control frame, tolerating a closed connection.

        Used where delivery is best-effort — terminal reports, error
        frames, drain notices — so a vanished client never masks the
        terminal sequence.

        Args:
            frame: The typed server frame to offer.
        """
        try:
            await self._send_frame(frame)
        except ConnectionClosed:
            self._logger.debug(
                "Frame not delivered; connection already closed",
                extra={"frame_type": type(frame).__name__},
            )

    async def _refuse_connection(self, message: str) -> None:
        """Refuse the connection with an ``internal`` error frame and close.

        Used for pre-lifecycle failures: a first frame that is not
        ``session.start``, or a Session_Store failure before the session
        went live.

        Args:
            message: Human-readable refusal message for the error frame.
        """
        await self._send_quietly(
            ErrorFrame(
                category=ErrorCategory.INTERNAL,
                message=message,
                recoverable=False,
            )
        )
        await self._close_connection(CLOSE_CODE_ERROR, "session-refused")

    async def _close_connection(self, code: int, reason: str) -> None:
        """Close the WebSocket, tolerating an already-closed connection.

        Args:
            code: WebSocket close code.
            reason: Short close reason token.
        """
        try:
            await self._connection.close(code, reason)
        except (ConnectionClosed, OSError):
            self._logger.debug(
                "WebSocket close after the peer was already gone",
                extra={"close_code": code},
            )

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    def _iso_now(self) -> str:
        """Render the injected wall clock as an ISO-8601 UTC timestamp.

        Returns:
            The current instant with a ``Z`` suffix (for example
            ``2024-01-01T00:00:00Z``), the shape the domain modules
            expect for injected timestamps.
        """
        rendered = datetime.fromtimestamp(self._clock(), tz=UTC).isoformat()
        return rendered.replace("+00:00", "Z")

    def _ttl(self) -> int:
        """Compute the Session_Store TTL for a write happening now (Req 8.4).

        Returns:
            The DynamoDB ``ttl`` attribute value: the injected wall clock
            plus the configured retention period, in epoch seconds.
        """
        return compute_ttl_from_epoch(int(self._clock()), self._retention_days)
