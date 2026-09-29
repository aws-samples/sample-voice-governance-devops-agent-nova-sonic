"""Deterministic in-memory fakes for the Voice_Service port interfaces (Req 17.6).

One fake per port abstract base class, plus a controllable clock:

- :class:`FakeBedrockStream` for ``BedrockStreamPort``
- :class:`FakeDevOpsAgent` for ``DevOpsAgentPort``
- :class:`FakeGuardrail` for ``GuardrailPort``
- :class:`FakeSessionStore` for ``SessionStorePort``
- :class:`FakeTaskProtection` for ``TaskProtectionPort``
- :class:`FakeClock` for injected time (the domain never reads a clock)

Every property and unit test of the suite drives orchestration through
these fakes instead of AWS: this module imports no SDK, performs no
network or file I/O, and is fully deterministic — outputs are scripted
by the test, recordings preserve exact call order, and time only moves
when :meth:`FakeClock.advance` is called.

Common conventions across the fakes:

- **Recording**: public list attributes capture calls in arrival order.
  Failure-injected calls are recorded too (the attempt happened), except
  where an attribute's name promises success (for example
  ``FakeBedrockStream.opened_events`` records successful opens only).
- **Failure injection**: scripted failures raise the exact portal
  exception class the real adapter would raise (``StreamOpenError``,
  ``BedrockStreamError``, ``AgentRequestError``,
  ``GuardrailUnavailableError``, ``SessionStoreError``,
  ``TaskProtectionError``) so orchestration error paths are exercised
  end to end.
- **Scripting**: outputs a fake produces (stream output events, agent
  response chunks, guardrail evaluations) are seeded by the test before
  or while driving the code under test.
"""

import asyncio
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from datetime import UTC, datetime
from typing import Final

from app.domain.guardrail_policy import ACTION_NONE, GuardrailEvaluation
from app.domain.segmentation import ReplayEvent
from app.domain.session import VoiceSession
from app.domain.transcript import TranscriptEntry
from app.exceptions import (
    AgentRequestError,
    BedrockStreamError,
    GuardrailUnavailableError,
    SessionStoreError,
    StreamOpenError,
    TaskProtectionError,
)
from app.ports.bedrock_stream import BedrockStreamPort
from app.ports.devops_agent import DevOpsAgentPort
from app.ports.guardrail import GuardrailPort
from app.ports.session_store import SessionStorePort, WebPushSubscription
from app.ports.task_protection import TaskProtectionPort

__all__ = [
    "DEFAULT_CLOCK_START",
    "FakeBedrockStream",
    "FakeClock",
    "FakeDevOpsAgent",
    "FakeGuardrail",
    "FakeSessionStore",
    "FakeTaskProtection",
]

DEFAULT_CLOCK_START: Final = 1_704_067_200.0
"""Default :class:`FakeClock` start instant: 2024-01-01T00:00:00Z in epoch seconds."""


class FakeClock:
    """Controllable deterministic clock for injected timestamps.

    One counter serves as both the monotonic and the wall clock: it
    starts at a fixed epoch instant and moves only when :meth:`advance`
    is called, so timing-sensitive logic (segmentation boundaries, the
    task-protection 60-second release bound, TTL computation inputs) is
    tested without real waiting. :meth:`now` feeds code that measures
    durations; :meth:`iso_now` feeds the domain modules that take
    ISO-8601 UTC timestamp strings.
    """

    def __init__(self, start: float = DEFAULT_CLOCK_START) -> None:
        """Initialize the clock at a fixed instant.

        Args:
            start: Initial clock value in epoch seconds; defaults to
                :data:`DEFAULT_CLOCK_START` so ISO timestamps render as
                stable 2024 dates.
        """
        self._now = start

    def now(self) -> float:
        """Return the current clock value in epoch seconds.

        Returns:
            The start instant plus every :meth:`advance` applied so far.
        """
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: Amount of time to add to the clock, in seconds;
                use non-negative values to keep the clock monotonic.
        """
        self._now += seconds

    def iso_now(self) -> str:
        """Return the current instant as an ISO-8601 UTC string.

        Returns:
            The current clock value rendered as an ISO-8601 UTC
            timestamp with a ``Z`` suffix (for example
            ``2024-01-01T00:00:00Z``), the shape the domain modules
            expect for injected timestamps.
        """
        rendered = datetime.fromtimestamp(self._now, tz=UTC).isoformat()
        return rendered.replace("+00:00", "Z")


class FakeBedrockStream(BedrockStreamPort):
    """In-memory ``BedrockStreamPort`` recording inputs and scripting outputs.

    Inputs are recorded exactly as sent; outputs are seeded by the test
    through :meth:`feed` and terminated with :meth:`end`, and
    :meth:`BedrockStreamPort.receive` yields them in feed order (FIFO
    queue), so ordering assertions are deterministic. Failure modes
    raise the same exception classes as the real adapter.

    Attributes:
        fail_open: When ``True``, :meth:`open` raises ``StreamOpenError``
            (Req 1.6) and records nothing.
        fail_send: When ``True``, :meth:`send_event`, :meth:`send_audio`,
            and :meth:`send_tool_result` raise ``BedrockStreamError``
            and record nothing.
        fail_mid_stream: When ``True``, the :meth:`receive` iterator
            raises ``BedrockStreamError`` once the scripted output
            events are exhausted (at the :meth:`end` or :meth:`close`
            sentinel) instead of finishing cleanly.
        opened_events: Event sequence of each successful :meth:`open`
            call, in call order — one tuple per opened stream, for
            replay-ordering assertions (Req 2.3).
        sent_events: Every event successfully passed to
            :meth:`send_event`, in call order.
        sent_audio: Every successful :meth:`send_audio` call as a
            ``(prompt_name, content_name, pcm)`` tuple, in call order.
        sent_tool_results: Every successful :meth:`send_tool_result`
            call as a ``(prompt_name, tool_use_id, content)`` tuple, in
            call order.
        closed: ``True`` once :meth:`close` has been called.
    """

    def __init__(
        self,
        *,
        fail_open: bool = False,
        fail_send: bool = False,
        fail_mid_stream: bool = False,
    ) -> None:
        """Initialize an idle fake stream with empty recordings.

        Args:
            fail_open: Initial value of the :attr:`fail_open` flag.
            fail_send: Initial value of the :attr:`fail_send` flag.
            fail_mid_stream: Initial value of the :attr:`fail_mid_stream`
                flag.
        """
        self.fail_open = fail_open
        self.fail_send = fail_send
        self.fail_mid_stream = fail_mid_stream
        self.opened_events: list[tuple[ReplayEvent, ...]] = []
        self.sent_events: list[ReplayEvent] = []
        self.sent_audio: list[tuple[str, str, bytes]] = []
        self.sent_tool_results: list[tuple[str, str, str]] = []
        self.closed = False
        self._outputs: asyncio.Queue[Mapping[str, object] | None] = asyncio.Queue()

    def feed(self, event: Mapping[str, object]) -> None:
        """Script one output event for :meth:`receive` to yield.

        Args:
            event: A decoded Nova Sonic output event (a JSON-ready
                mapping discriminated by its ``"type"`` key), yielded to
                the consumer in feed order.
        """
        self._outputs.put_nowait(event)

    def end(self) -> None:
        """Mark the end of the scripted output events.

        After all previously fed events are consumed, the
        :meth:`receive` iterator finishes cleanly — or raises
        ``BedrockStreamError`` when :attr:`fail_mid_stream` is set.
        """
        self._outputs.put_nowait(None)

    async def open(self, events: Sequence[ReplayEvent]) -> None:
        """Record the initial event sequence of an opened stream.

        Args:
            events: Initial input events in delivery order, as built by
                ``domain.segmentation.build_replay_sequence``; recorded
                as one tuple in :attr:`opened_events`.

        Raises:
            StreamOpenError: If :attr:`fail_open` is set (Req 1.6);
                nothing is recorded.
        """
        if self.fail_open:
            raise StreamOpenError("fail_open")
        self.opened_events.append(tuple(events))

    async def send_event(self, event: ReplayEvent) -> None:
        """Record one input event sent into the stream.

        Args:
            event: The input event, appended verbatim to
                :attr:`sent_events`.

        Raises:
            BedrockStreamError: If :attr:`fail_send` is set; nothing is
                recorded.
        """
        if self.fail_send:
            raise BedrockStreamError("fail_send")
        self.sent_events.append(event)

    async def send_audio(
        self, prompt_name: str, content_name: str, pcm: bytes
    ) -> None:
        """Record one audio chunk sent into the stream.

        Args:
            prompt_name: Prompt identifier of the live stream.
            content_name: Name of the open audio content block.
            pcm: Raw PCM bytes exactly as passed by the caller; recorded
                unencoded so byte-identity assertions (Req 2.4) compare
                the original payload.

        Raises:
            BedrockStreamError: If :attr:`fail_send` is set; nothing is
                recorded.
        """
        if self.fail_send:
            raise BedrockStreamError("fail_send")
        self.sent_audio.append((prompt_name, content_name, pcm))

    async def send_tool_result(
        self, prompt_name: str, tool_use_id: str, content: str
    ) -> None:
        """Record one tool result returned to the stream.

        Args:
            prompt_name: Prompt identifier of the live stream.
            tool_use_id: The ``toolUseId`` the result answers.
            content: The stringified JSON tool-result payload.

        Raises:
            BedrockStreamError: If :attr:`fail_send` is set; nothing is
                recorded.
        """
        if self.fail_send:
            raise BedrockStreamError("fail_send")
        self.sent_tool_results.append((prompt_name, tool_use_id, content))

    async def receive(self) -> AsyncIterator[Mapping[str, object]]:
        """Yield the scripted output events in feed order.

        Awaits the internal FIFO queue, so a consumer task suspends
        until the test feeds more events or ends the script — letting
        tests interleave feeding with consumption deterministically.

        Yields:
            Each event previously passed to :meth:`feed`, in order.

        Raises:
            BedrockStreamError: When the script is exhausted (the
                :meth:`end` or :meth:`close` sentinel is reached) and
                :attr:`fail_mid_stream` is set — the scripted mid-stream
                failure.
        """
        while True:
            item = await self._outputs.get()
            if item is None:
                if self.fail_mid_stream:
                    raise BedrockStreamError("fail_mid_stream")
                return
            yield item

    async def close(self) -> None:
        """Record teardown and end the output script.

        Sets :attr:`closed` and enqueues the end sentinel so a pending
        :meth:`receive` iterator finishes after draining already-fed
        events, mirroring the real adapter's teardown (Req 2.5).
        Closing again is a no-op.
        """
        if self.closed:
            return
        self.closed = True
        self._outputs.put_nowait(None)


class FakeDevOpsAgent(DevOpsAgentPort):
    """In-memory ``DevOpsAgentPort`` with deterministic ids and scripted chunks.

    Chat identifiers are ``chat-1``, ``chat-2``, ... in creation order,
    so chat-reuse assertions (Req 3.2, 3.9) compare stable values.
    Responses are scripted per chat via :meth:`respond_on` or globally
    via :meth:`respond_with`; :meth:`DevOpsAgentPort.send_message`
    yields the scripted chunks in order.

    Because :meth:`send_message` is implemented as an async generator,
    its recording and failure behavior run on the first iteration step,
    matching the port contract that ``AgentRequestError`` may be raised
    by the iterator (Req 3.7).

    Attributes:
        fail_create: When ``True``, :meth:`create_chat` raises
            ``AgentRequestError`` after recording the attempt.
        fail_send: When ``True``, :meth:`send_message` raises
            ``AgentRequestError`` on first iteration after recording the
            attempt.
        hang_forever: When ``True``, :meth:`send_message` suspends
            before its first chunk on an event that is never set. The
            wait is cancellable, so ``asyncio.timeout`` around
            consumption expires normally — the 60-second-budget test
            path (Req 3.8).
        chunk_delay: Optional async callback awaited before each yielded
            chunk — an injection point for per-chunk pacing (for
            example advancing a clock or yielding control) in timeout
            tests. ``None`` (the default) yields chunks back to back.
        create_calls: The ``execution_id`` of every :meth:`create_chat`
            call in call order, including failure-injected attempts —
            execution-scoping assertions read this list (Req 3.5, 3.6).
        send_calls: Every :meth:`send_message` iteration start as a
            ``(chat_id, text)`` tuple in call order, including
            failure-injected attempts.
    """

    def __init__(
        self,
        *,
        fail_create: bool = False,
        fail_send: bool = False,
        hang_forever: bool = False,
    ) -> None:
        """Initialize an agent fake with no scripted responses.

        Args:
            fail_create: Initial value of the :attr:`fail_create` flag.
            fail_send: Initial value of the :attr:`fail_send` flag.
            hang_forever: Initial value of the :attr:`hang_forever` flag.
        """
        self.fail_create = fail_create
        self.fail_send = fail_send
        self.hang_forever = hang_forever
        self.chunk_delay: Callable[[], Awaitable[None]] | None = None
        self.create_calls: list[str | None] = []
        self.send_calls: list[tuple[str, str]] = []
        self._chats_created = 0
        self._default_chunks: tuple[str, ...] = ()
        self._chat_chunks: dict[str, tuple[str, ...]] = {}
        self._never_set = asyncio.Event()

    def respond_with(self, chunks: Iterable[str]) -> None:
        """Script the global response used for chats without their own script.

        Args:
            chunks: Response chunks yielded in order by every subsequent
                :meth:`send_message` whose chat has no per-chat script.
        """
        self._default_chunks = tuple(chunks)

    def respond_on(self, chat_id: str, chunks: Iterable[str]) -> None:
        """Script the response for one specific chat.

        Args:
            chat_id: Chat whose :meth:`send_message` calls yield these
                chunks, taking precedence over the global script.
            chunks: Response chunks yielded in order.
        """
        self._chat_chunks[chat_id] = tuple(chunks)

    async def create_chat(self, execution_id: str | None) -> str:
        """Record the creation call and return the next deterministic id.

        Args:
            execution_id: Execution scope of the chat, appended to
                :attr:`create_calls` (``None`` for an unscoped chat,
                Req 3.5, 3.6).

        Returns:
            ``chat-1`` for the first successful creation, ``chat-2`` for
            the second, and so on.

        Raises:
            AgentRequestError: If :attr:`fail_create` is set (Req 3.7);
                the attempt is still recorded and the id counter does
                not advance.
        """
        self.create_calls.append(execution_id)
        if self.fail_create:
            raise AgentRequestError("CreateChat")
        self._chats_created += 1
        return f"chat-{self._chats_created}"

    async def send_message(self, chat_id: str, text: str) -> AsyncIterator[str]:
        """Yield the scripted response chunks for one request.

        Args:
            chat_id: Chat the request is sent on; selects the per-chat
                script when one exists, the global script otherwise.
            text: The engineer request text, recorded with the chat id
                in :attr:`send_calls`.

        Yields:
            The scripted chunks in order, each preceded by an await of
            :attr:`chunk_delay` when set.

        Raises:
            AgentRequestError: If :attr:`fail_send` is set (Req 3.7);
                raised on the first iteration step, after recording.
        """
        self.send_calls.append((chat_id, text))
        if self.fail_send:
            raise AgentRequestError("SendMessage")
        if self.hang_forever:
            await self._never_set.wait()
        for chunk in self._chat_chunks.get(chat_id, self._default_chunks):
            if self.chunk_delay is not None:
                await self.chunk_delay()
            yield chunk


class FakeGuardrail(GuardrailPort):
    """In-memory ``GuardrailPort`` returning scripted evaluations.

    By default every call returns :attr:`default_evaluation` — a clean
    pass (``action == "NONE"``, no findings) that
    ``domain.guardrail_policy.decide`` maps to PASS. Tests script other
    outcomes by queueing evaluations (consumed one per call, in order)
    or by switching the fake into its unavailable mode.

    Attributes:
        default_evaluation: Evaluation returned when the script queue is
            empty; reassign it for a persistent non-default outcome.
        unavailable_reason: When not ``None``, every :meth:`evaluate`
            call raises ``GuardrailUnavailableError`` with this reason
            (Req 4.6), taking precedence over queued evaluations. Set
            via :meth:`raise_unavailable`; reset by assigning ``None``.
        evaluations: Every text passed to :meth:`evaluate`, in call
            order, including calls that raised.
    """

    def __init__(self) -> None:
        """Initialize a guardrail fake that passes everything by default."""
        self.default_evaluation = GuardrailEvaluation(action=ACTION_NONE)
        self.unavailable_reason: str | None = None
        self.evaluations: list[str] = []
        self._scripted: deque[GuardrailEvaluation] = deque()

    def queue_evaluation(self, *evaluations: GuardrailEvaluation) -> None:
        """Queue evaluations to return, one per subsequent call, in order.

        Args:
            evaluations: Parsed evaluations to return before falling
                back to :attr:`default_evaluation`.
        """
        self._scripted.extend(evaluations)

    def raise_unavailable(self, reason: str = "scripted") -> None:
        """Switch the fake into its unavailable failure mode.

        Every subsequent :meth:`evaluate` call raises
        ``GuardrailUnavailableError`` until :attr:`unavailable_reason`
        is reset to ``None``.

        Args:
            reason: Failure reason carried by the raised error, feeding
                the fail-closed ``unavailable:<reason>`` BLOCK path
                (Req 4.6).
        """
        self.unavailable_reason = reason

    async def evaluate(self, text: str) -> GuardrailEvaluation:
        """Record the evaluated text and produce the next scripted outcome.

        Args:
            text: The engineer request text, appended to
                :attr:`evaluations` before any outcome is produced.

        Returns:
            The next queued evaluation when one exists, otherwise
            :attr:`default_evaluation`.

        Raises:
            GuardrailUnavailableError: If :attr:`unavailable_reason` is
                set (Req 4.6).
        """
        self.evaluations.append(text)
        if self.unavailable_reason is not None:
            raise GuardrailUnavailableError(self.unavailable_reason)
        if self._scripted:
            return self._scripted.popleft()
        return self.default_evaluation


class FakeSessionStore(SessionStorePort):
    """Dict-backed ``SessionStorePort`` with a call log and failure injection.

    The four Session_Store tables are plain dictionaries the test can
    inspect directly, and :attr:`calls` records every method call (name
    and arguments) in call order — the evidence for persist-before-
    confirm ordering assertions (Req 8.2, 8.7, design Property 11).

    Failure injection raises ``SessionStoreError`` exactly as the real
    adapter would (Req 8.5): persistently per method group via
    :attr:`fail_reads` / :attr:`fail_writes`, or for the next N calls of
    one method via :meth:`fail_next` (for bounded-retry tests, Req 2.7).
    Failing calls are recorded in :attr:`calls` but never mutate a
    table, so a failure leaves no partial record.

    Attributes:
        sessions: voice-sessions table — Voice_Session snapshots keyed
            by ``session_id`` (Req 8.1).
        chat_ids: agent-chats table — ``(chat_id, execution_id)`` keyed
            by ``session_id`` (Req 3.2), preserving the scoping the
            mapping was persisted with (Req 3.5, 3.6).
        transcripts: transcripts table — entries per ``session_id`` in
            append order (Req 2.5).
        subscriptions: push-subscriptions table — records keyed by
            ``(engineer_id, endpoint_hash)`` (Req 6.1).
        ttls: ``(method_name, ttl)`` for every successful write that
            carried a TTL, in call order (Req 8.4).
        calls: ``(method_name, args)`` for every call in call order,
            including failure-injected calls; ``args`` echoes the
            call's arguments as a tuple.
        fail_reads: When ``True``, every read method raises.
        fail_writes: When ``True``, every write method raises.
    """

    def __init__(self) -> None:
        """Initialize an empty store with failure injection disabled."""
        self.sessions: dict[str, VoiceSession] = {}
        self.chat_ids: dict[str, tuple[str, str | None]] = {}
        self.transcripts: dict[str, list[TranscriptEntry]] = {}
        self.subscriptions: dict[tuple[str, str], WebPushSubscription] = {}
        self.ttls: list[tuple[str, int]] = []
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_reads = False
        self.fail_writes = False
        self._fail_next: dict[str, int] = {}

    def fail_next(self, method_name: str, times: int = 1) -> None:
        """Make the next calls of one method fail, then recover.

        Args:
            method_name: Port method to fail, for example
                ``"append_transcript"``.
            times: How many subsequent calls of the method fail before
                it succeeds again; additive across repeated
                :meth:`fail_next` calls.
        """
        self._fail_next[method_name] = self._fail_next.get(method_name, 0) + times

    def _record(
        self,
        method: str,
        args: tuple[object, ...],
        *,
        write: bool,
        session_id: str | None = None,
    ) -> None:
        """Log one call and raise if a failure is injected for it.

        Args:
            method: Name of the port method being called.
            args: The call's arguments, echoed into :attr:`calls`.
            write: Whether the method belongs to the write group
                (checked against :attr:`fail_writes`) or the read group
                (checked against :attr:`fail_reads`).
            session_id: Session the operation is scoped to, carried by
                the raised error; ``None`` for unscoped operations.

        Raises:
            SessionStoreError: If a one-shot :meth:`fail_next` budget or
                the matching group flag is active for this call
                (Req 8.5).
        """
        self.calls.append((method, args))
        remaining = self._fail_next.get(method, 0)
        if remaining > 0:
            self._fail_next[method] = remaining - 1
            raise SessionStoreError(method, session_id)
        group_failed = self.fail_writes if write else self.fail_reads
        if group_failed:
            raise SessionStoreError(method, session_id)

    async def get_session(self, session_id: str) -> VoiceSession | None:
        """Read one Voice_Session snapshot.

        Args:
            session_id: Identifier of the Voice_Session to read.

        Returns:
            The stored snapshot, or ``None`` when absent (Req 8.6).

        Raises:
            SessionStoreError: If a read failure is injected (Req 8.5).
        """
        self._record("get_session", (session_id,), write=False, session_id=session_id)
        return self.sessions.get(session_id)

    async def put_session(self, session: VoiceSession, *, ttl: int) -> None:
        """Upsert one Voice_Session snapshot and record its TTL.

        Args:
            session: The snapshot to store under ``session.session_id``.
            ttl: TTL attribute value in epoch seconds, recorded in
                :attr:`ttls` (Req 8.4).

        Raises:
            SessionStoreError: If a write failure is injected; the table
                is left unchanged (Req 8.5).
        """
        self._record(
            "put_session",
            (session, ttl),
            write=True,
            session_id=session.session_id,
        )
        self.sessions[session.session_id] = session
        self.ttls.append(("put_session", ttl))

    async def get_chat_id(self, session_id: str) -> str | None:
        """Read the chat identifier mapped to a session.

        Args:
            session_id: Identifier of the owning Voice_Session.

        Returns:
            The persisted chat identifier (Req 3.9), or ``None`` when no
            mapping exists (Req 3.10).

        Raises:
            SessionStoreError: If a read failure is injected (Req 8.5).
        """
        self._record("get_chat_id", (session_id,), write=False, session_id=session_id)
        mapping = self.chat_ids.get(session_id)
        return mapping[0] if mapping is not None else None

    async def put_chat_id(
        self,
        session_id: str,
        chat_id: str,
        *,
        ttl: int,
        execution_id: str | None = None,
    ) -> None:
        """Persist the chat mapping of a session and record its TTL.

        Args:
            session_id: Identifier of the owning Voice_Session.
            chat_id: Chat identifier to persist (Req 3.2).
            ttl: TTL attribute value in epoch seconds, recorded in
                :attr:`ttls` (Req 8.4).
            execution_id: Execution scope the chat was created with, or
                ``None`` for an unscoped chat; stored alongside the chat
                id for scoping assertions (Req 3.5, 3.6).

        Raises:
            SessionStoreError: If a write failure is injected; the table
                is left unchanged (Req 8.5).
        """
        self._record(
            "put_chat_id",
            (session_id, chat_id, ttl, execution_id),
            write=True,
            session_id=session_id,
        )
        self.chat_ids[session_id] = (chat_id, execution_id)
        self.ttls.append(("put_chat_id", ttl))

    async def append_transcript(
        self, session_id: str, entry: TranscriptEntry, *, ttl: int
    ) -> None:
        """Append one transcript entry for a session and record its TTL.

        Args:
            session_id: Identifier of the owning Voice_Session.
            entry: The transcript line to persist (Req 2.5).
            ttl: TTL attribute value in epoch seconds, recorded in
                :attr:`ttls` (Req 8.4).

        Raises:
            SessionStoreError: If a write failure is injected; the table
                is left unchanged (Req 8.5).
        """
        self._record(
            "append_transcript",
            (session_id, entry, ttl),
            write=True,
            session_id=session_id,
        )
        self.transcripts.setdefault(session_id, []).append(entry)
        self.ttls.append(("append_transcript", ttl))

    async def get_transcript(self, session_id: str) -> list[TranscriptEntry]:
        """Read all transcript entries of a session in ``seq`` order.

        Args:
            session_id: Identifier of the Voice_Session whose transcript
                to read.

        Returns:
            A new list of the session's entries sorted by ascending
            ``seq`` (Req 8.3); empty when none were appended.

        Raises:
            SessionStoreError: If a read failure is injected (Req 8.5).
        """
        self._record(
            "get_transcript", (session_id,), write=False, session_id=session_id
        )
        entries = self.transcripts.get(session_id, [])
        return sorted(entries, key=lambda entry: entry.seq)

    async def put_subscription(self, subscription: WebPushSubscription) -> None:
        """Upsert one Web_Push_Subscription record.

        Args:
            subscription: The record to store, keyed by
                ``(engineer_id, endpoint_hash)`` so re-registering the
                same endpoint is idempotent (Req 6.1).

        Raises:
            SessionStoreError: If a write failure is injected; the table
                is left unchanged (Req 6.7, 8.5).
        """
        self._record("put_subscription", (subscription,), write=True)
        key = (subscription.engineer_id, subscription.endpoint_hash)
        self.subscriptions[key] = subscription

    async def delete_subscription(
        self, engineer_id: str, endpoint_hash: str
    ) -> None:
        """Remove one Web_Push_Subscription record; absent is a no-op.

        Args:
            engineer_id: Partition key of the record to remove.
            endpoint_hash: Sort key of the record to remove (Req 6.5).

        Raises:
            SessionStoreError: If a write failure is injected; the table
                is left unchanged (Req 8.5).
        """
        self._record(
            "delete_subscription", (engineer_id, endpoint_hash), write=True
        )
        self.subscriptions.pop((engineer_id, endpoint_hash), None)

    async def list_subscriptions(self) -> list[WebPushSubscription]:
        """Read all Web_Push_Subscription records in insertion order.

        Returns:
            A new list of every stored subscription (Req 6.2); empty
            when none are stored.

        Raises:
            SessionStoreError: If a read failure is injected (Req 8.5).
        """
        self._record("list_subscriptions", (), write=False)
        return list(self.subscriptions.values())


class FakeTaskProtection(TaskProtectionPort):
    """In-memory ``TaskProtectionPort`` recording timestamped transitions.

    Every :meth:`acquire` and :meth:`release` attempt is recorded with
    the injected :class:`FakeClock`'s current time, so timing bounds —
    release within 60 seconds of the session count reaching zero
    (Req 10.4, 19.7) and rolling expiry refreshes — are assertable
    without real waiting (design Property 17).

    Attributes:
        fail_acquire: Number of upcoming :meth:`acquire` calls that
            raise ``TaskProtectionError`` before succeeding again — set
            to N for fail-N-times-then-succeed retry tests (Req 10.7),
            or to a value above the caller's retry budget to exhaust it.
        fail_release: Same failure budget for :meth:`release`.
        calls: Every attempt as ``(operation, expiry_minutes, time)`` in
            call order — ``("acquire", minutes, clock.now())`` or
            ``("release", None, clock.now())`` — including failed
            attempts.
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        fail_acquire: int = 0,
        fail_release: int = 0,
    ) -> None:
        """Initialize an unprotected fake bound to a test clock.

        Args:
            clock: Clock supplying the timestamps recorded in
                :attr:`calls`.
            fail_acquire: Initial :attr:`fail_acquire` failure budget.
            fail_release: Initial :attr:`fail_release` failure budget.
        """
        self._clock = clock
        self.fail_acquire = fail_acquire
        self.fail_release = fail_release
        self.calls: list[tuple[str, int | None, float]] = []
        self._protected = False

    @property
    def protected(self) -> bool:
        """Return whether the task is currently protected.

        Returns:
            ``True`` after a successful :meth:`acquire` and until a
            successful :meth:`release`; failed attempts never change it.
        """
        return self._protected

    async def acquire(self, expiry_minutes: int) -> None:
        """Record an acquire attempt and mark the task protected.

        Calling acquire while already protected models the rolling
        expiry refresh (Req 10.3): the attempt is recorded with its
        timestamp and the task stays protected.

        Args:
            expiry_minutes: Protection expiry recorded with the attempt.

        Raises:
            TaskProtectionError: If the :attr:`fail_acquire` budget is
                positive; the budget decrements and the protection state
                is unchanged (Req 10.7).
        """
        self.calls.append(("acquire", expiry_minutes, self._clock.now()))
        if self.fail_acquire > 0:
            self.fail_acquire -= 1
            raise TaskProtectionError("acquire")
        self._protected = True

    async def release(self) -> None:
        """Record a release attempt and mark the task unprotected.

        Releasing an unprotected task is a no-op that is still recorded,
        matching the port contract (Req 10.4).

        Raises:
            TaskProtectionError: If the :attr:`fail_release` budget is
                positive; the budget decrements and the protection state
                is unchanged (Req 10.7).
        """
        self.calls.append(("release", None, self._clock.now()))
        if self.fail_release > 0:
            self.fail_release -= 1
            raise TaskProtectionError("release")
        self._protected = False
