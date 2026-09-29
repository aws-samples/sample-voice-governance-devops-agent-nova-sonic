# Feature: nova-sonic-support-portal, Property 11: State changes persist before they are confirmed
"""Property test: state changes persist before they are confirmed.

**Validates: Requirements 8.2, 8.7**

For any sequence of Voice_Session state transitions — creation, going
live, any number of segmentation rollovers, and both graceful end modes —
every ``session.state`` confirmation frame and every ``transcript`` frame
the client receives is preceded, in one globally ordered event log, by
the completed Session_Store write it reports (Req 8.2, design
Property 11). Reporting is one-directional: a completed write whose
confirmation cannot be delivered (a vanished client, an exhausted
transcript-persistence retry) is legal, while a confirmation without its
completed write is the violation this suite exists to catch.

Requirement 8.7 states the same contract for Web_Push_Subscription
registration and removal: the composition root returns its 204
confirmation strictly after the ``SessionStorePort`` write returned.
``app.main`` builds the application from a full configuration
environment at import time, so — per the design's testing strategy of
driving orchestration through the port fakes — this suite asserts the
shared persist-before-confirm discipline at the seam the design
designates for properties: the session manager writing through
``SessionStorePort`` and confirming over the ``VoiceConnection``.

Each example draws an interleaving of operations applied to one live
session served by
:class:`~app.orchestration.voice_session_manager.VoiceSessionManager`:

- **transcript** — one Nova Sonic ``textOutput`` event (drawn role and
  unicode text) fed into the live fake stream; the manager must persist
  the entry before relaying the ``transcript`` frame. A drawn failure
  flag makes the store fail all four persistence attempts instead, in
  which case the frame must be withheld — no confirmation without a
  completed write.
- **segment** — one segmentation rollover (``LIVE → SEGMENTING → LIVE``)
  triggered by granting the segmentation timer one rollover permit; each
  of the two transitions must persist before its state frame.

The evidence is a single ordered event log shared by two recorders: a
``FakeSessionStore`` subclass appends a ``persist`` entry only after the
underlying write returned (a failure-injected call appends nothing — the
write never completed), and the scripted ``VoiceConnection`` fake
appends a ``confirm`` entry for every ``session.state`` and
``transcript`` frame it is asked to send. The core assertion scans the
log once per kind: every confirmation must consume a matching earlier
completed write (first-match on payload), so confirming before
persisting — or confirming a withheld entry — fails. Shape assertions
then pin the machinery the example exercised: the persisted state
sequence equals ``connecting, live, (segmenting, live) × rollovers,
ended``; the confirmed sequence equals the persisted one, minus the
terminal ``ended`` when the client disconnected first (the persist still
happens; delivery is impossible); persisted and confirmed transcript
lines equal exactly the successful feeds in order; and the durable store
agrees (terminal ``ENDED`` snapshot, transcript table content, one fresh
Bedrock_Stream per rollover).

Determinism: all injected clocks are one :class:`tests.fakes.FakeClock`,
and the manager's injected sleep discriminates callers by requested
delay — retry backoff (jittered, at most 3 s) returns instantly, the
segmentation timer's rollover delay (450 s) parks on a permit queue and
advances the clock when granted (so each permit yields exactly one
rollover), and the token-expiry watchdog's delay (a 10^9-second token
lifetime) parks forever, cancellably. The protection manager gets a
separate park-or-instant sleep so its 300-second refresh loop never
touches the permit queue. Operations are sequenced with bounded
cooperative yield loops — every fake settles without real waiting — and
``hypothesis.given`` cannot drive ``async def`` tests under
pytest-asyncio, so each example runs its coroutine to completion with
``asyncio.run`` from a synchronous test body, the same pattern as the
Property 7, 14, and 17 suites.
"""

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.auth.jwt_validator import ValidatedToken
from app.domain.segmentation import ROLLOVER_STREAM_AGE_SECONDS
from app.domain.session import SessionState, VoiceSession
from app.domain.transcript import Role, TranscriptEntry
from app.orchestration.drain_manager import DrainManager
from app.orchestration.protection_manager import ProtectionManager
from app.orchestration.tool_router import ToolRouter
from app.orchestration.voice_session_manager import (
    CLOSE_CODE_NORMAL,
    KEEPALIVE_POLL_SECONDS,
    TRANSCRIPT_PERSIST_RETRIES,
    ConnectionClosed,
    VoiceSessionManager,
)
from app.protocol.ws_messages import (
    SESSION_END_TYPE,
    SESSION_START_TYPE,
    SESSION_STATE_TYPE,
    TRANSCRIPT_TYPE,
)
from tests.fakes import (
    FakeBedrockStream,
    FakeClock,
    FakeDevOpsAgent,
    FakeGuardrail,
    FakeSessionStore,
    FakeTaskProtection,
)

_PERSIST: Final = "persist"
"""Event-log channel of a completed Session_Store write."""

_CONFIRM: Final = "confirm"
"""Event-log channel of a confirmation frame offered to the client."""

_KIND_SESSION: Final = "session"
"""Event-log kind for session-state writes and ``session.state`` frames."""

_KIND_TRANSCRIPT: Final = "transcript"
"""Event-log kind for transcript writes and ``transcript`` frames."""

_SYSTEM_PROMPT: Final = "You are the read-only diagnostic voice assistant."
"""Minimal system prompt handed to the manager; replayed on every stream."""

_TOKEN_LIFETIME_SECONDS: Final = 1_000_000_000.0
"""Token lifetime so the expiry watchdog's delay always parks forever."""

_INSTANT_BELOW_SECONDS: Final = 10.0
"""Delays below this return instantly: retry backoff tops out near 3 s."""

_PARK_AT_OR_ABOVE_SECONDS: Final = 1_000_000.0
"""Delays at or above this park forever: the token watchdog's horizon."""

_ROLLOVER_SECONDS: Final = ROLLOVER_STREAM_AGE_SECONDS
"""Rollover threshold (450 s): between the instant and park bands, so the
segmentation timer's sleep is the only permit-gated delay."""

_REFRESH_INTERVAL_SECONDS: Final = 300.0
"""Protection refresh interval; parked forever by the protection sleep."""

_TRANSCRIPT_ATTEMPTS: Final = TRANSCRIPT_PERSIST_RETRIES + 1
"""Persistence attempts per transcript entry (1 + 3 retries, Req 2.7)."""

_APPEND_TRANSCRIPT_METHOD: Final = "append_transcript"
"""Store method name observed in the fake's call log for attempt counts."""

_MAX_OPS: Final = 6
"""Maximum number of drawn operations per example."""

_MAX_TEXT_LENGTH: Final = 30
"""Maximum length of a drawn transcript line."""

_MAX_ENGINEER_ID_LENGTH: Final = 12
"""Maximum length of the drawn engineer identity."""

_MAX_SETTLE_ROUNDS: Final = 2_000
"""Bound on cooperative yield rounds while waiting for one operation to
settle; every fake resolves in a handful of rounds, so exhausting this
bound means the session manager stalled."""

_SESSION_START_JSON: Final = json.dumps({"type": SESSION_START_TYPE})
"""Opening client frame: a plain, unscoped ``session.start``."""

_SESSION_END_JSON: Final = json.dumps({"type": SESSION_END_TYPE})
"""Graceful-end client frame (Req 2.5)."""

type _LogEntry = tuple[str, str, object]
"""One ordered event: ``(channel, kind, payload)`` — channel is
``persist`` or ``confirm``, kind is ``session`` or ``transcript``."""


@dataclass(frozen=True, slots=True)
class _TranscriptOp:
    """One ``textOutput`` relay through the persist-then-confirm path.

    Attributes:
        role: Speaker role carried by the fed ``textOutput`` event.
        text: Non-empty unicode transcript line.
        persist_fails: When ``True``, the store fails all persistence
            attempts for this entry, so its ``transcript`` frame must be
            withheld (Req 8.2).
    """

    role: Role
    text: str
    persist_fails: bool


@dataclass(frozen=True, slots=True)
class _SegmentOp:
    """One segmentation rollover: ``LIVE → SEGMENTING → LIVE`` (Req 2.2)."""


type _Op = _TranscriptOp | _SegmentOp
"""One drawn operation applied to the live session."""

_OPS_STRATEGY: Final = st.lists(
    st.one_of(
        st.builds(
            _TranscriptOp,
            role=st.sampled_from(Role),
            text=st.text(min_size=1, max_size=_MAX_TEXT_LENGTH),
            persist_fails=st.booleans(),
        ),
        st.just(_SegmentOp()),
    ),
    max_size=_MAX_OPS,
)
"""Interleavings of transcript relays (with drawn persistence failures)
and segmentation rollovers."""


class _ScriptedConnection:
    """Scripted ``VoiceConnection`` fake recording confirmations in order.

    Implements the session manager's transport protocol over an inbound
    frame queue the test feeds. Every ``session.state`` and
    ``transcript`` frame the manager sends is appended to the shared
    event log as a ``confirm`` entry at the moment of the send — the
    instant the state change is reported to the client (Req 8.2). After
    :meth:`close` or :meth:`drop_peer`, sends and receives raise
    :class:`ConnectionClosed`, matching the protocol contract.

    Attributes:
        state_frame_session_ids: ``sessionId`` of every ``session.state``
            frame sent, in send order.
        close_calls: Every ``(code, reason)`` close call, in call order.
    """

    def __init__(self, log: list[_LogEntry]) -> None:
        """Initialize an open connection bound to the shared event log.

        Args:
            log: Shared ordered event log receiving ``confirm`` entries.
        """
        self._log = log
        self._incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._peer_gone = False
        self._closed = False
        self.state_frame_session_ids: list[object] = []
        self.close_calls: list[tuple[int, str]] = []

    def deliver(self, frame: str | bytes) -> None:
        """Queue one client frame for the manager to receive.

        Args:
            frame: Text control frame (``str``) or binary PCM (``bytes``).
        """
        self._incoming.put_nowait(frame)

    def drop_peer(self) -> None:
        """Model the client vanishing: receives and sends start failing.

        A pending or subsequent :meth:`receive` raises
        :class:`ConnectionClosed`, and every subsequent send raises too —
        so a terminal confirmation can no longer be delivered while the
        terminal persist still happens (the one-directional facet of the
        property).
        """
        self._peer_gone = True
        self._incoming.put_nowait(None)

    async def send_text(self, data: str) -> None:
        """Record the confirmation carried by one server text frame.

        Args:
            data: Serialized JSON control frame from the manager.

        Raises:
            ConnectionClosed: If the connection is closed or the peer is
                gone; nothing is recorded — the confirmation was never
                delivered.
        """
        if self._closed or self._peer_gone:
            raise ConnectionClosed("send-text-after-close")
        payload = json.loads(data)
        frame_type = payload.get("type")
        if frame_type == SESSION_STATE_TYPE:
            self._log.append((_CONFIRM, _KIND_SESSION, payload["state"]))
            self.state_frame_session_ids.append(payload["sessionId"])
        elif frame_type == TRANSCRIPT_TYPE:
            self._log.append(
                (_CONFIRM, _KIND_TRANSCRIPT, (payload["role"], payload["text"]))
            )

    async def send_bytes(self, data: bytes) -> None:
        """Accept one binary audio frame (never produced by this suite).

        Args:
            data: Raw PCM audio bytes.

        Raises:
            ConnectionClosed: If the connection is closed or the peer is
                gone.
        """
        if self._closed or self._peer_gone:
            raise ConnectionClosed("send-bytes-after-close")

    async def receive(self) -> str | bytes:
        """Await the next scripted client frame.

        Returns:
            The next frame passed to :meth:`deliver`, in delivery order.

        Raises:
            ConnectionClosed: When the connection was closed locally or
                the peer was dropped; the end sentinel is re-queued so
                every later receive raises as well.
        """
        if self._closed:
            raise ConnectionClosed("receive-after-close")
        item = await self._incoming.get()
        if item is None:
            self._incoming.put_nowait(None)
            raise ConnectionClosed("peer-gone")
        return item

    async def close(self, code: int, reason: str) -> None:
        """Record the close; closing an already-closed connection is safe.

        Args:
            code: WebSocket close code.
            reason: Short close reason token.
        """
        self.close_calls.append((code, reason))
        self._closed = True
        self._incoming.put_nowait(None)


class _OrderRecordingStore(FakeSessionStore):
    """``FakeSessionStore`` appending completed writes to the event log.

    A ``persist`` entry joins the shared log only after the parent
    fake's write returned — a failure-injected call raises first and
    appends nothing, so the log records exactly the writes that
    *completed* (Req 8.2, 8.5). All recording, table, and
    failure-injection behavior is inherited unchanged.
    """

    def __init__(self, log: list[_LogEntry]) -> None:
        """Initialize an empty store bound to the shared event log.

        Args:
            log: Shared ordered event log receiving ``persist`` entries.
        """
        super().__init__()
        self._log = log

    async def put_session(self, session: VoiceSession, *, ttl: int) -> None:
        """Persist one session snapshot, then log the completed write.

        Args:
            session: The snapshot to store.
            ttl: TTL attribute value in epoch seconds.

        Raises:
            SessionStoreError: If a write failure is injected; nothing is
                logged because the write never completed.
        """
        await super().put_session(session, ttl=ttl)
        self._log.append((_PERSIST, _KIND_SESSION, session.state.ws_state))

    async def append_transcript(
        self, session_id: str, entry: TranscriptEntry, *, ttl: int
    ) -> None:
        """Persist one transcript entry, then log the completed write.

        Args:
            session_id: Identifier of the owning Voice_Session.
            entry: The transcript line to persist.
            ttl: TTL attribute value in epoch seconds.

        Raises:
            SessionStoreError: If a write failure is injected; nothing is
                logged because the write never completed.
        """
        await super().append_transcript(session_id, entry, ttl=ttl)
        self._log.append(
            (_PERSIST, _KIND_TRANSCRIPT, (entry.role.value.lower(), entry.text))
        )


class _SleepRouter:
    """Routes the manager's injected sleep by the requested delay.

    The input keepalive is matched by its exact interval
    (:data:`~app.orchestration.voice_session_manager.KEEPALIVE_POLL_SECONDS`)
    and parked, since holding the Bedrock stream open through idle input is
    outside this property. Three delay bands cover the rest: retry backoff
    (below :data:`_INSTANT_BELOW_SECONDS`) returns instantly, the token
    watchdog's near-infinite delay (at or above
    :data:`_PARK_AT_OR_ABOVE_SECONDS`) parks forever on a cancellable
    wait, and the segmentation timer's rollover delay (the middle band)
    parks on a permit queue — :meth:`allow_rollover` grants one permit,
    the sleep advances the shared clock by the requested delay, and the
    timer wakes exactly at its threshold, so each permit yields exactly
    one segmentation rollover.
    """

    def __init__(self, clock: FakeClock) -> None:
        """Initialize the router around the shared test clock.

        Args:
            clock: Clock advanced when a rollover permit is granted.
        """
        self._clock = clock
        self._rollover_permits: asyncio.Queue[None] = asyncio.Queue()

    def allow_rollover(self) -> None:
        """Grant the segmentation timer one rollover permit."""
        self._rollover_permits.put_nowait(None)

    async def sleep(self, delay: float) -> None:
        """Serve one injected sleep according to its delay band.

        Args:
            delay: Requested delay in seconds; selects the band.
        """
        if delay == KEEPALIVE_POLL_SECONDS:
            # The input keepalive is irrelevant to this property and must
            # not consume a rollover permit; park it cancellably.
            await asyncio.Event().wait()
            return
        if delay < _INSTANT_BELOW_SECONDS:
            return
        if delay >= _PARK_AT_OR_ABOVE_SECONDS:
            await asyncio.Event().wait()
            return
        await self._rollover_permits.get()
        self._clock.advance(delay)


async def _protection_sleep(delay: float) -> None:
    """Protection-manager sleep: instant backoff, parked refresh loop.

    Kept separate from :class:`_SleepRouter` so the protection manager's
    300-second refresh interval never consumes a rollover permit; the
    park is cancellable, so ``session_ended`` tearing the refresh task
    down always proceeds.

    Args:
        delay: Requested delay in seconds; delays at or above
            :data:`_INSTANT_BELOW_SECONDS` park forever.
    """
    if delay >= _INSTANT_BELOW_SECONDS:
        await asyncio.Event().wait()


def _quiet_logger() -> logging.Logger:
    """Build a logger that swallows the manager's lifecycle entries.

    The suite asserts on the event log and the durable store, not on log
    output, so a dedicated non-propagating logger with a ``NullHandler``
    keeps expected retry warnings and exhaustion records out of the test
    output without touching global logging configuration.

    Returns:
        A reusable, isolated, silent logger.
    """
    logger = logging.getLogger("tests.property.p11")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def _sequential_ids() -> Callable[[], str]:
    """Build a deterministic identifier factory for the manager.

    Returns:
        A zero-argument factory yielding ``id-1``, ``id-2``, ... for the
        session identifier, prompt names, and content names.
    """
    counter = itertools.count(1)

    def factory() -> str:
        """Return the next sequential identifier.

        Returns:
            The next ``id-<n>`` string.
        """
        return f"id-{next(counter)}"

    return factory


def _reaches(count: Callable[[], int], target: int) -> Callable[[], bool]:
    """Build a settle predicate that holds once a count reaches a target.

    Values are bound as arguments instead of being closed over, so
    predicates built inside the operation loop never capture loop
    variables.

    Args:
        count: Zero-argument callable observing the current count.
        target: Count at which the predicate starts holding.

    Returns:
        A predicate returning ``True`` once ``count()`` is at least
        ``target``.
    """

    def predicate() -> bool:
        """Return whether the observed count reached the bound target.

        Returns:
            ``True`` once the count is at or past the target.
        """
        return count() >= target

    return predicate


async def _settle(predicate: Callable[[], bool], description: str) -> None:
    """Yield cooperatively until the predicate holds.

    Every collaborator is an in-memory fake, so each pending step of the
    session's task group progresses on every yielded round; the bound
    exists only to convert a stalled manager into a test failure instead
    of a hang.

    Args:
        predicate: Condition to wait for.
        description: What was being waited for, for the failure message.
    """
    for _ in range(_MAX_SETTLE_ROUNDS):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate(), f"the session manager stalled before: {description}"


def _payloads(log: list[_LogEntry], channel: str, kind: str) -> list[object]:
    """Project the log onto one channel and kind, preserving order.

    Args:
        log: The shared ordered event log.
        channel: ``persist`` or ``confirm``.
        kind: ``session`` or ``transcript``.

    Returns:
        The payloads of the matching entries, in log order.
    """
    return [
        payload
        for entry_channel, entry_kind, payload in log
        if entry_channel == channel and entry_kind == kind
    ]


def _confirm_count(log: list[_LogEntry], kind: str) -> int:
    """Count the confirmations of one kind recorded so far.

    Args:
        log: The shared ordered event log.
        kind: ``session`` or ``transcript``.

    Returns:
        The number of ``confirm`` entries of that kind.
    """
    return len(_payloads(log, _CONFIRM, kind))


def _append_attempts(store: FakeSessionStore) -> int:
    """Count transcript-persistence attempts, including failed ones.

    Args:
        store: The fake store whose call log to scan.

    Returns:
        The number of ``append_transcript`` calls recorded so far.
    """
    return sum(
        1 for method, _ in store.calls if method == _APPEND_TRANSCRIPT_METHOD
    )


def _text_output_event(op: _TranscriptOp) -> dict[str, object]:
    """Build the ``textOutput`` stream event for one transcript operation.

    Args:
        op: The drawn transcript operation.

    Returns:
        A decoded Nova Sonic ``textOutput`` event carrying the drawn role
        and text.
    """
    return {"type": "textOutput", "role": op.role.value, "content": op.text}


def _assert_confirms_consume_completed_writes(
    log: list[_LogEntry], kind: str
) -> None:
    """Assert every confirmation follows its own completed store write.

    Scans the log in order, collecting completed writes of ``kind``;
    every confirmation must consume a matching earlier write
    (first-match on payload), so a confirmation emitted before its write
    completed — or one whose write never completed — fails. Completed
    writes without a confirmation are legal: reporting is
    one-directional (Req 8.2).

    Args:
        log: The shared ordered event log.
        kind: ``session`` or ``transcript``.
    """
    completed: list[object] = []
    for channel, entry_kind, payload in log:
        if entry_kind != kind:
            continue
        if channel == _PERSIST:
            completed.append(payload)
        else:
            assert payload in completed, (
                f"{kind} change {payload!r} was confirmed to the client"
                " before its Session_Store write completed"
            )
            completed.remove(payload)


async def _check_interleaving(
    ops: list[_Op], *, end_via_disconnect: bool, engineer_id: str
) -> None:
    """Drive one drawn interleaving and assert Property 11 on the log.

    Serves one full session lifetime through a real
    :class:`VoiceSessionManager` wired to fakes, applies the drawn
    operations in order (settling each before the next), ends the
    session by client request or by dropping the peer, and then asserts
    the ordering property plus the expected shape of both event
    sequences and the durable store.

    Args:
        ops: Drawn interleaving of transcript and segmentation
            operations.
        end_via_disconnect: Whether the session ends by the peer
            vanishing (terminal persist without a deliverable
            confirmation) instead of a ``session.end`` frame.
        engineer_id: Drawn engineer identity owning the session.

    Raises:
        AssertionError: If any facet of Property 11 is violated.
    """
    log: list[_LogEntry] = []
    clock = FakeClock()
    store = _OrderRecordingStore(log)
    streams: list[FakeBedrockStream] = []
    quiet = _quiet_logger()
    router = _SleepRouter(clock)

    def stream_factory() -> FakeBedrockStream:
        """Open a fresh fake Bedrock_Stream and track it.

        Returns:
            The new stream; the test feeds output events into the most
            recently opened one.
        """
        stream = FakeBedrockStream()
        streams.append(stream)
        return stream

    manager = VoiceSessionManager(
        stream_factory=stream_factory,
        store=store,
        tool_router=ToolRouter(
            FakeGuardrail(),
            FakeDevOpsAgent(),
            store,
            clock=clock.now,
            logger=quiet,
        ),
        protection=ProtectionManager(
            FakeTaskProtection(clock),
            refresh_interval_seconds=_REFRESH_INTERVAL_SECONDS,
            logger=quiet,
            sleep=_protection_sleep,
        ),
        drain=DrainManager(logger=quiet),
        system_prompt=_SYSTEM_PROMPT,
        rollover_seconds=_ROLLOVER_SECONDS,
        clock=clock.now,
        monotonic=clock.now,
        sleep=router.sleep,
        logger=quiet,
        id_factory=_sequential_ids(),
    )
    token = ValidatedToken(
        sub=engineer_id,
        username=engineer_id,
        expires_at=clock.now() + _TOKEN_LIFETIME_SECONDS,
        claims={},
    )
    connection = _ScriptedConnection(log)
    connection.deliver(_SESSION_START_JSON)
    run_task = asyncio.create_task(manager.run_session(connection, token))

    try:
        session_confirms = partial(_confirm_count, log, _KIND_SESSION)
        transcript_confirms = partial(_confirm_count, log, _KIND_TRANSCRIPT)
        append_attempts = partial(_append_attempts, store)
        await _settle(
            _reaches(session_confirms, 2),
            "the session reported connecting and live",
        )
        expected_session_confirms = 2
        expected_lines: list[tuple[str, str]] = []
        for op in ops:
            if isinstance(op, _SegmentOp):
                expected_session_confirms += 2
                router.allow_rollover()
                await _settle(
                    _reaches(session_confirms, expected_session_confirms),
                    "the rollover reported segmenting and live",
                )
            elif op.persist_fails:
                attempts_target = append_attempts() + _TRANSCRIPT_ATTEMPTS
                store.fail_next(_APPEND_TRANSCRIPT_METHOD, _TRANSCRIPT_ATTEMPTS)
                streams[-1].feed(_text_output_event(op))
                await _settle(
                    _reaches(append_attempts, attempts_target),
                    "the transcript persistence retries were exhausted",
                )
            else:
                confirm_target = transcript_confirms() + 1
                streams[-1].feed(_text_output_event(op))
                await _settle(
                    _reaches(transcript_confirms, confirm_target),
                    "the transcript line was confirmed",
                )
                expected_lines.append((op.role.value.lower(), op.text))
        if end_via_disconnect:
            connection.drop_peer()
        else:
            connection.deliver(_SESSION_END_JSON)
        await run_task
    finally:
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

    rollovers = sum(1 for op in ops if isinstance(op, _SegmentOp))
    expected_states: list[object] = [
        SessionState.CONNECTING.ws_state,
        SessionState.LIVE.ws_state,
    ]
    for _ in range(rollovers):
        expected_states.extend(
            (SessionState.SEGMENTING.ws_state, SessionState.LIVE.ws_state)
        )
    expected_states.append(SessionState.ENDED.ws_state)

    assert _payloads(log, _PERSIST, _KIND_SESSION) == expected_states
    expected_confirmed = (
        expected_states[:-1] if end_via_disconnect else expected_states
    )
    assert _payloads(log, _CONFIRM, _KIND_SESSION) == expected_confirmed
    assert _payloads(log, _PERSIST, _KIND_TRANSCRIPT) == expected_lines
    assert _payloads(log, _CONFIRM, _KIND_TRANSCRIPT) == expected_lines

    _assert_confirms_consume_completed_writes(log, _KIND_SESSION)
    _assert_confirms_consume_completed_writes(log, _KIND_TRANSCRIPT)

    assert len(store.sessions) == 1
    session_id = next(iter(store.sessions))
    assert store.sessions[session_id].state is SessionState.ENDED
    stored_lines = [
        (entry.role.value.lower(), entry.text)
        for entry in store.transcripts.get(session_id, [])
    ]
    assert stored_lines == expected_lines
    assert set(connection.state_frame_session_ids) == {session_id}
    assert len(streams) == 1 + rollovers
    assert connection.close_calls
    assert connection.close_calls[-1][0] == CLOSE_CODE_NORMAL


@given(
    ops=_OPS_STRATEGY,
    end_via_disconnect=st.booleans(),
    engineer_id=st.text(min_size=1, max_size=_MAX_ENGINEER_ID_LENGTH),
)
@settings(max_examples=100, deadline=None)
def test_state_changes_persist_before_confirmation(
    ops: list[_Op],
    end_via_disconnect: bool,
    engineer_id: str,
) -> None:
    """Every confirmation is preceded by its completed Session_Store write.

    For every drawn interleaving of transcript relays (with and without
    injected persistence failures) and segmentation rollovers, ended by
    a client request or a peer disconnect: every ``session.state`` and
    ``transcript`` frame offered to the client consumes a matching
    earlier completed store write in the global event log (Req 8.2,
    design Property 11); the persisted state sequence is exactly
    ``connecting, live, (segmenting, live) × rollovers, ended``;
    entries whose persistence exhausted its retries are never confirmed;
    a disconnect withholds only the terminal confirmation while its
    persist still completes; and the durable store holds the terminal
    snapshot and exactly the successfully persisted transcript lines.

    Args:
        ops: Drawn interleaving of transcript and segmentation
            operations.
        end_via_disconnect: Drawn end mode — peer disconnect versus a
            graceful ``session.end`` frame.
        engineer_id: Drawn engineer identity owning the session.
    """
    asyncio.run(
        _check_interleaving(
            ops,
            end_via_disconnect=end_via_disconnect,
            engineer_id=engineer_id,
        )
    )
