# Feature: nova-sonic-support-portal, Property 12: Reconnect restores exactly what was persisted
"""Property test: reconnect restores exactly what was persisted.

**Validates: Requirements 8.3**

For any persisted Voice_Session state and transcript entry list,
disconnecting and reconnecting to that session restores session state
and the complete transcript list, equal in content and order to what was
persisted (Property 12; Req 8.3).

The main property seeds :class:`tests.fakes.FakeSessionStore` with an
arbitrary resumable session snapshot (any non-terminal state, any
scoping, any segment count) and an arbitrary transcript list — seeded in
shuffled order to prove restoration orders by ``seq``, not by storage
order — then drives
:class:`app.orchestration.voice_session_manager.VoiceSessionManager` end
to end over a scripted connection presenting ``session.start`` with a
``resumeSessionId`` followed by ``session.end``. It asserts exact
restoration:

- the fresh Bedrock_Stream is opened exactly once, with a replay whose
  history block sequence equals the persisted entries sorted by ``seq``
  — same roles, same texts, same order, nothing lost, nothing invented
  (Req 8.3);
- the restored snapshot persisted back to the Session_Store carries the
  persisted identity, scoping, and segment count verbatim, re-entering
  at ``CONNECTING`` and then moving to ``LIVE`` (one more segment) and
  ``ENDED``; every state frame reports the persisted session id;
- the store sees exactly one session read, one transcript read, and the
  three lifecycle writes, and the seeded transcript rows are untouched.

The companion property covers the rejection half of the reconnect
contract (Req 8.6): for a ``resumeSessionId`` that is unknown, names a
terminal session, or names a session owned by a different engineer, the
manager answers with exactly one ``session_not_found`` error frame,
closes the connection, opens no Bedrock_Stream, and writes nothing to
the Session_Store.

Determinism: every collaborator is an in-memory fake; the injected sleep
parks forever (cancellably), so the segmentation timer and the
token-expiry watchdog never fire; the fake clock never advances, so
every injected timestamp equals the clock's start instant.
``hypothesis.given`` cannot drive ``async def`` tests under
pytest-asyncio, so each example runs its coroutine to completion with
``asyncio.run`` from a synchronous test body — the same pattern as the
Property 7, 14, and 17 suites, giving every example a fresh event loop.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import count
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import DrawFn

from app.auth.jwt_validator import ValidatedToken
from app.domain.session import IncidentContext, SessionState, VoiceSession
from app.domain.transcript import Role, TranscriptEntry
from app.orchestration.drain_manager import DrainManager
from app.orchestration.protection_manager import ProtectionManager
from app.orchestration.tool_router import ToolRouter
from app.orchestration.voice_session_manager import (
    CLOSE_CODE_ERROR,
    CLOSE_CODE_NORMAL,
    SESSION_NOT_FOUND_MESSAGE,
    ConnectionClosed,
    VoiceSessionManager,
)
from app.ports.bedrock_stream import BedrockStreamPort
from app.protocol.ws_messages import ERROR_TYPE, SESSION_STATE_TYPE, ErrorCategory
from tests.fakes import (
    FakeBedrockStream,
    FakeClock,
    FakeDevOpsAgent,
    FakeGuardrail,
    FakeSessionStore,
    FakeTaskProtection,
)

_FAR_FUTURE_SECONDS: Final = 1_000_000.0
"""Token-expiry offset keeping the mid-session watchdog parked forever."""

_END_FRAME: Final = json.dumps({"type": "session.end"})
"""Scripted ``session.end`` client frame ending the resumed session."""

_MAX_ENTRIES: Final = 20
"""Largest persisted transcript drawn per example."""

_RESUMABLE_STATES: Final = (
    SessionState.CONNECTING,
    SessionState.LIVE,
    SessionState.SEGMENTING,
)
"""Persisted states a reconnect may resume from (non-terminal, Req 8.6)."""

_TERMINAL_STATES: Final = (SessionState.ENDED, SessionState.ERROR)
"""Persisted states whose sessions can no longer be resumed (Req 8.6)."""

_IDS: Final[st.SearchStrategy[str]] = st.text(min_size=1, max_size=32)
"""Identifiers (session ids, engineer subs, execution ids): any short text."""

_TIMESTAMPS: Final[st.SearchStrategy[str]] = st.text(min_size=1, max_size=40)
"""Opaque persisted timestamp strings, carried verbatim through restore."""

_INCIDENT_CONTEXTS: Final[st.SearchStrategy[IncidentContext | None]] = st.one_of(
    st.none(),
    st.builds(
        IncidentContext,
        summary=st.text(max_size=40),
        severity=st.text(max_size=20),
    ),
)
"""Optional incident scoping carried by the persisted session (Req 5.8)."""


@dataclass(frozen=True)
class _RestoreScenario:
    """One resumable persisted session with its seeded transcript.

    Attributes:
        session: Persisted Voice_Session snapshot seeded into the store;
            always resumable (non-terminal state) and owned by the
            reconnecting engineer.
        seeded_entries: Persisted transcript entries in the (shuffled)
            order they are seeded into the store; restoration must order
            them by ``seq``, not by this storage order.
        system_prompt: System-prompt template configured on the manager.
    """

    session: VoiceSession
    seeded_entries: tuple[TranscriptEntry, ...]
    system_prompt: str


@dataclass(frozen=True)
class _RejectionScenario:
    """One reconnect attempt that must be rejected (Req 8.6).

    Attributes:
        seeded_session: Session snapshot seeded into the store; depending
            on the drawn rejection reason it is terminal, owned by a
            different engineer, or simply not the one being resumed.
        seeded_entries: Transcript entries seeded for the stored session;
            must remain untouched by the rejected attempt.
        resume_session_id: The ``resumeSessionId`` the client presents.
        token_sub: Cognito subject of the reconnecting engineer's token.
    """

    seeded_session: VoiceSession
    seeded_entries: tuple[TranscriptEntry, ...]
    resume_session_id: str
    token_sub: str


@st.composite
def _persisted_transcripts(draw: DrawFn) -> list[TranscriptEntry]:
    """Draw a persisted transcript list with strictly increasing ``seq``.

    Sequence numbers are drawn as a unique set and sorted, matching the
    domain invariant that ``seq`` is strictly monotonically increasing
    per session (gaps allowed); roles, texts, and timestamps are
    arbitrary.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        Up to ``_MAX_ENTRIES`` entries in ascending ``seq`` order; the
        list the reconnect must restore exactly.
    """
    bodies = draw(
        st.lists(
            st.tuples(
                st.sampled_from(Role),
                st.text(max_size=40),
                st.text(min_size=1, max_size=40),
            ),
            max_size=_MAX_ENTRIES,
        )
    )
    seqs = draw(
        st.lists(
            st.integers(min_value=0, max_value=1_000_000),
            min_size=len(bodies),
            max_size=len(bodies),
            unique=True,
        )
    )
    return [
        TranscriptEntry(seq=seq, role=role, text=text, timestamp=timestamp)
        for seq, (role, text, timestamp) in zip(sorted(seqs), bodies, strict=True)
    ]


@st.composite
def _restore_scenarios(draw: DrawFn) -> _RestoreScenario:
    """Draw one resumable persisted session and its shuffled transcript.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        A scenario whose session carries an arbitrary non-terminal state,
        arbitrary scoping (executionId and/or incident context), and an
        arbitrary segment count, with the transcript seeded in a drawn
        permutation of its ``seq`` order.
    """
    entries = draw(_persisted_transcripts())
    session = VoiceSession(
        session_id=draw(_IDS),
        engineer_id=draw(_IDS),
        created_at=draw(_TIMESTAMPS),
        updated_at=draw(_TIMESTAMPS),
        state=draw(st.sampled_from(_RESUMABLE_STATES)),
        execution_id=draw(st.none() | _IDS),
        incident_context=draw(_INCIDENT_CONTEXTS),
        segment_count=draw(st.integers(min_value=0, max_value=10)),
    )
    return _RestoreScenario(
        session=session,
        seeded_entries=tuple(draw(st.permutations(entries))),
        system_prompt=draw(st.text(min_size=1, max_size=40)),
    )


@st.composite
def _rejection_scenarios(draw: DrawFn) -> _RejectionScenario:
    """Draw one non-resumable reconnect attempt (Req 8.6).

    Three rejection reasons are drawn with equal weight: an unknown
    ``resumeSessionId`` (the store holds only a different session), a
    known session in a terminal state, and a known non-terminal session
    owned by a different engineer.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        A scenario whose resume attempt the manager must reject with
        ``session_not_found``.
    """
    reason = draw(st.sampled_from(("unknown", "terminal", "foreign")))
    session_id, other_id = draw(
        st.lists(_IDS, min_size=2, max_size=2, unique=True)
    )
    token_sub, foreign_sub = draw(
        st.lists(_IDS, min_size=2, max_size=2, unique=True)
    )
    if reason == "terminal":
        state = draw(st.sampled_from(_TERMINAL_STATES))
        owner = token_sub
        resume_id = session_id
    elif reason == "foreign":
        state = draw(st.sampled_from(_RESUMABLE_STATES))
        owner = foreign_sub
        resume_id = session_id
    else:
        state = draw(st.sampled_from(tuple(SessionState)))
        owner = draw(st.sampled_from((token_sub, foreign_sub)))
        resume_id = other_id
    session = VoiceSession(
        session_id=session_id,
        engineer_id=owner,
        created_at=draw(_TIMESTAMPS),
        updated_at=draw(_TIMESTAMPS),
        state=state,
        execution_id=draw(st.none() | _IDS),
        incident_context=draw(_INCIDENT_CONTEXTS),
        segment_count=draw(st.integers(min_value=0, max_value=10)),
    )
    return _RejectionScenario(
        seeded_session=session,
        seeded_entries=tuple(draw(_persisted_transcripts())),
        resume_session_id=resume_id,
        token_sub=token_sub,
    )


class _ScriptedConnection:
    """Scripted :class:`VoiceConnection` fake recording everything sent.

    Serves the queued inbound frames one per :meth:`receive` call; once
    the script is exhausted — or after :meth:`close` — every receive or
    send raises :class:`ConnectionClosed`, modelling a client that stays
    connected exactly as long as the script runs.

    Attributes:
        sent_texts: Every text frame sent by the manager, in send order.
        sent_bytes: Every binary frame sent by the manager, in send order.
        closes: Every ``(code, reason)`` close call, in call order.
    """

    def __init__(self, frames: Sequence[str]) -> None:
        """Initialize the connection with its scripted inbound frames.

        Args:
            frames: Client text frames served in order by
                :meth:`receive`.
        """
        self._inbound: deque[str] = deque(frames)
        self._closed = False
        self.sent_texts: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closes: list[tuple[int, str]] = []

    async def send_text(self, data: str) -> None:
        """Record one text frame sent to the client.

        Args:
            data: Serialized JSON control frame.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        if self._closed:
            raise ConnectionClosed("closed")
        self.sent_texts.append(data)

    async def send_bytes(self, data: bytes) -> None:
        """Record one binary frame sent to the client.

        Args:
            data: Raw PCM audio bytes.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        if self._closed:
            raise ConnectionClosed("closed")
        self.sent_bytes.append(data)

    async def receive(self) -> str | bytes:
        """Serve the next scripted client frame.

        Returns:
            The next queued text frame.

        Raises:
            ConnectionClosed: When the script is exhausted or the
                connection was closed.
        """
        if self._closed or not self._inbound:
            raise ConnectionClosed("script-exhausted")
        return self._inbound.popleft()

    async def close(self, code: int, reason: str) -> None:
        """Record the close call and mark the connection closed.

        Args:
            code: WebSocket close code.
            reason: Short close reason token.
        """
        self.closes.append((code, reason))
        self._closed = True


async def _park_sleep(_delay: float) -> None:
    """Park forever (cancellably) in place of any timer sleep.

    Injected as the manager's, the protection manager's, and the drain
    manager's sleep so the segmentation timer, the token-expiry
    watchdog, the protection refresh loop, and retry backoff never fire
    during an example; the wait is cancellable, so task-group teardown
    and ``asyncio.run`` always unwind cleanly.

    Args:
        _delay: Requested delay in seconds; ignored.
    """
    await asyncio.Event().wait()


def _quiet_logger() -> logging.Logger:
    """Build a logger that swallows the manager's lifecycle entries.

    The suite asserts on frames, store calls, and stream recordings, not
    on logs, so a dedicated non-propagating logger with a ``NullHandler``
    keeps expected lifecycle entries out of the test output without
    touching global logging configuration.

    Returns:
        A reusable, isolated, silent logger.
    """
    logger = logging.getLogger("tests.property.p12")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def _sequential_ids() -> Callable[[], str]:
    """Build a deterministic identifier factory for the manager.

    Returns:
        A zero-argument callable yielding ``gen-1``, ``gen-2``, ... so
        generated prompt and content names never depend on real UUIDs.
    """
    counter = count(1)

    def _next_id() -> str:
        """Return the next sequential generated identifier.

        Returns:
            The next ``gen-N`` identifier.
        """
        return f"gen-{next(counter)}"

    return _next_id


def _make_manager(
    *,
    streams: list[FakeBedrockStream],
    store: FakeSessionStore,
    clock: FakeClock,
    system_prompt: str,
) -> VoiceSessionManager:
    """Wire a session manager to fakes with fully deterministic timing.

    Args:
        streams: List receiving every stream the factory creates, in
            creation order, so the test can count opens and inspect the
            replay.
        store: Fake Session_Store already seeded by the test.
        clock: Fake clock serving as both the wall and monotonic clock;
            never advanced, so every timestamp is the start instant.
        system_prompt: System-prompt template for the manager.

    Returns:
        A manager whose collaborators (tool router over fake ports,
        protection manager over a fake port, drain manager) perform no
        I/O and whose timers are parked by :func:`_park_sleep`.
    """
    logger = _quiet_logger()

    def _factory() -> BedrockStreamPort:
        """Create and record one fresh fake Bedrock stream.

        Returns:
            The new stream, also appended to the enclosing list.
        """
        stream = FakeBedrockStream()
        streams.append(stream)
        return stream

    return VoiceSessionManager(
        stream_factory=_factory,
        store=store,
        tool_router=ToolRouter(
            FakeGuardrail(),
            FakeDevOpsAgent(),
            store,
            clock=clock.now,
            logger=logger,
        ),
        protection=ProtectionManager(
            FakeTaskProtection(clock), logger=logger, sleep=_park_sleep
        ),
        drain=DrainManager(sleep=_park_sleep, logger=logger),
        system_prompt=system_prompt,
        clock=clock.now,
        monotonic=clock.now,
        sleep=_park_sleep,
        logger=logger,
        id_factory=_sequential_ids(),
    )


def _token_for(sub: str, clock: FakeClock) -> ValidatedToken:
    """Build a validated token owned by ``sub`` that never expires mid-test.

    Args:
        sub: Cognito subject identifier the token authenticates.
        clock: Fake clock whose current instant anchors the far-future
            expiry.

    Returns:
        A token whose ``expires_at`` lies far beyond the never-advancing
        clock, keeping the expiry watchdog parked.
    """
    return ValidatedToken(
        sub=sub,
        username=sub,
        expires_at=clock.now() + _FAR_FUTURE_SECONDS,
        claims={},
    )


def _start_frame(resume_session_id: str) -> str:
    """Serialize the ``session.start`` frame resuming one session.

    Args:
        resume_session_id: The ``resumeSessionId`` to present.

    Returns:
        The frame's wire JSON text.
    """
    return json.dumps(
        {"type": "session.start", "resumeSessionId": resume_session_id}
    )


def _sent_frames(connection: _ScriptedConnection) -> list[dict[str, object]]:
    """Decode every control frame the manager sent, in send order.

    Args:
        connection: The scripted connection that recorded the frames.

    Returns:
        Each sent text frame parsed as its wire JSON object.

    Raises:
        AssertionError: If a sent frame is not a JSON object.
    """
    frames: list[dict[str, object]] = []
    for text in connection.sent_texts:
        decoded: object = json.loads(text)
        assert isinstance(decoded, dict)
        frames.append(decoded)
    return frames


async def _check_restoration(scenario: _RestoreScenario) -> None:
    """Drive one resume end to end and assert exact restoration (Req 8.3).

    Args:
        scenario: Drawn persisted session, seeded transcript order, and
            manager configuration.

    Raises:
        AssertionError: If any facet of Property 12 is violated.
    """
    clock = FakeClock()
    store = FakeSessionStore()
    session_id = scenario.session.session_id
    store.sessions[session_id] = scenario.session
    store.transcripts[session_id] = list(scenario.seeded_entries)
    streams: list[FakeBedrockStream] = []
    manager = _make_manager(
        streams=streams,
        store=store,
        clock=clock,
        system_prompt=scenario.system_prompt,
    )
    connection = _ScriptedConnection([_start_frame(session_id), _END_FRAME])

    await manager.run_session(
        connection, _token_for(scenario.session.engineer_id, clock)
    )

    # The complete persisted transcript, restored in seq order: the fresh
    # stream is opened exactly once with a replay whose history block
    # (role, text) sequence equals the persisted entries — nothing lost,
    # nothing invented, order preserved (Req 8.3).
    expected_entries = sorted(scenario.seeded_entries, key=lambda entry: entry.seq)
    assert len(streams) == 1
    stream = streams[0]
    assert len(stream.opened_events) == 1
    replay = stream.opened_events[0]
    assert len(replay) == 2 + 3 * (1 + len(expected_entries))
    assert replay[0]["type"] == "sessionStart"
    assert replay[1]["type"] == "promptStart"
    history_events = replay[5:]
    observed: list[tuple[object, object]] = []
    for offset in range(0, len(history_events), 3):
        start, text_input, end = history_events[offset : offset + 3]
        assert start["type"] == "contentStart"
        assert text_input["type"] == "textInput"
        assert end["type"] == "contentEnd"
        observed.append((start["role"], text_input["content"]))
    assert observed == [(entry.role.value, entry.text) for entry in expected_entries]

    # No history is invented after the open: the only post-open stream
    # input is the interactive audio block, never another text block.
    assert all(event["type"] != "textInput" for event in stream.sent_events)
    assert stream.sent_audio == []
    assert stream.sent_tool_results == []
    assert stream.closed is True

    # The restored session context: identity, scoping, and segment count
    # carry over verbatim; the snapshot re-enters at CONNECTING and moves
    # LIVE (one more segment) and ENDED, each persisted before reported.
    now_iso = clock.iso_now()
    connecting = replace(
        scenario.session, state=SessionState.CONNECTING, updated_at=now_iso
    )
    live = replace(
        connecting,
        state=SessionState.LIVE,
        segment_count=scenario.session.segment_count + 1,
    )
    ended = replace(live, state=SessionState.ENDED)
    puts = [args[0] for method, args in store.calls if method == "put_session"]
    assert puts == [connecting, live, ended]
    assert store.sessions[session_id] == ended

    # Exactly one session read, one transcript read, three lifecycle
    # writes — and the seeded transcript rows are untouched.
    assert [method for method, _ in store.calls] == [
        "get_session",
        "get_transcript",
        "put_session",
        "put_session",
        "put_session",
    ]
    assert store.calls[0] == ("get_session", (session_id,))
    assert store.calls[1] == ("get_transcript", (session_id,))
    assert store.transcripts[session_id] == list(scenario.seeded_entries)

    # Client-visible restoration: every state frame reports the persisted
    # session id through connecting -> live -> ended; no error frame, no
    # invented transcript frames, and a normal close.
    frames = _sent_frames(connection)
    assert [frame["type"] for frame in frames] == [SESSION_STATE_TYPE] * 3
    assert all(frame["sessionId"] == session_id for frame in frames)
    assert [frame["state"] for frame in frames] == ["connecting", "live", "ended"]
    assert connection.sent_bytes == []
    assert connection.closes == [(CLOSE_CODE_NORMAL, "client_end")]


async def _check_rejection(scenario: _RejectionScenario) -> None:
    """Drive one non-resumable resume and assert the rejection (Req 8.6).

    Args:
        scenario: Drawn rejection attempt (unknown id, terminal session,
            or foreign owner) with the store's seeded contents.

    Raises:
        AssertionError: If the rejection leaks state, opens a stream, or
            reports anything but ``session_not_found``.
    """
    clock = FakeClock()
    store = FakeSessionStore()
    seeded = scenario.seeded_session
    store.sessions[seeded.session_id] = seeded
    store.transcripts[seeded.session_id] = list(scenario.seeded_entries)
    sessions_before = dict(store.sessions)
    transcripts_before = {
        session_id: list(entries)
        for session_id, entries in store.transcripts.items()
    }
    streams: list[FakeBedrockStream] = []
    manager = _make_manager(
        streams=streams, store=store, clock=clock, system_prompt="prompt"
    )
    connection = _ScriptedConnection([_start_frame(scenario.resume_session_id)])

    await manager.run_session(connection, _token_for(scenario.token_sub, clock))

    frames = _sent_frames(connection)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == ERROR_TYPE
    assert frame["category"] == ErrorCategory.SESSION_NOT_FOUND.value
    assert frame["message"] == SESSION_NOT_FOUND_MESSAGE
    assert frame["recoverable"] is False
    assert connection.sent_bytes == []
    assert connection.closes == [(CLOSE_CODE_ERROR, "session-not-found")]

    # No session is created or restored: no stream is opened, the store
    # sees only the single session read, and nothing was written.
    assert streams == []
    assert store.calls == [("get_session", (scenario.resume_session_id,))]
    assert store.sessions == sessions_before
    assert store.transcripts == transcripts_before


@given(scenario=_restore_scenarios())
@settings(max_examples=100, deadline=None)
def test_reconnect_restores_exactly_the_persisted_session(
    scenario: _RestoreScenario,
) -> None:
    """Resuming restores the persisted state and transcript exactly.

    For any persisted resumable Voice_Session and any persisted
    transcript list (seeded in any storage order), reconnecting with its
    ``resumeSessionId`` replays exactly the persisted entries in ``seq``
    order into one fresh Bedrock_Stream and carries the persisted session
    context (identity, scoping, segment count) through the restored
    lifecycle — nothing lost, nothing invented, order preserved
    (Req 8.3).

    Args:
        scenario: Drawn persisted session, shuffled transcript seeding
            order, and system prompt.
    """
    asyncio.run(_check_restoration(scenario))


@given(scenario=_rejection_scenarios())
@settings(max_examples=100, deadline=None)
def test_unavailable_resume_yields_session_not_found(
    scenario: _RejectionScenario,
) -> None:
    """A non-resumable ``resumeSessionId`` is rejected without side effects.

    For any resume attempt naming an unknown session, a terminal session,
    or a session owned by a different engineer, the manager sends exactly
    one ``session_not_found`` error frame and closes the connection,
    opening no Bedrock_Stream and leaving the Session_Store unread beyond
    the lookup and unwritten (Req 8.6).

    Args:
        scenario: Drawn rejection attempt with its seeded store contents.
    """
    asyncio.run(_check_rejection(scenario))
