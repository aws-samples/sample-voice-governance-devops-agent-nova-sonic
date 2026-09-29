"""Unit tests for the tool router's edge cases (Req 3.2, 3.7, 3.8, 3.10).

Branch-specific companions to the Property 7 gate suite, covering the
chat-lifecycle and failure paths of
:class:`app.orchestration.tool_router.ToolRouter`:

- **Chat lifecycle** — the first ``ask_devops_agent`` call of a session
  creates the chat with the session's execution scoping and persists the
  mapping with a TTL of the injected clock plus the retention period
  (Req 3.2, 3.5, 8.4); subsequent calls reuse the persisted chat
  (Req 3.9); a lost mapping is recovered by creating and re-persisting a
  new chat (Req 3.10).
- **Agent failures** — a failing ``CreateChat`` or ``SendMessage``
  returns the spoken :data:`AGENT_FAILURE_MESSAGE` error tool result and
  logs the exception class with the session identifier (Req 3.7); when
  ``CreateChat`` succeeded first, the mapping stays persisted.
- **Timeouts** — an agent that never produces a chunk, or stalls
  mid-stream after its first chunk, exhausts the configured budget: the
  router stops consuming promptly, returns the spoken
  :data:`AGENT_TIMEOUT_MESSAGE`, logs ``AgentTimeoutError``, and never
  leaks partial chunks into the result (Req 3.8).
- **Store failures** — a failing chat-mapping read is converted into the
  spoken failure result with ``SessionStoreError`` logged and no agent
  call (Req 8.5).
- **Query extraction** — blank, missing, or non-string queries
  short-circuit to :data:`EMPTY_QUERY_MESSAGE` without a guardrail call;
  each accepted input form (parsed mapping, stringified JSON, raw
  string) delivers its exact extracted query to the guardrail.

Every test drives the router end to end against the deterministic
in-memory fakes (:mod:`tests.fakes`) with an injected
:class:`FakeClock` and a private capturing logger — no AWS access, no
real waiting beyond the sub-second scripted timeouts.
"""

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import pytest

from app.domain.ttl import DEFAULT_RETENTION_DAYS, SECONDS_PER_DAY
from app.orchestration.tool_router import (
    AGENT_FAILURE_MESSAGE,
    AGENT_TIMEOUT_MESSAGE,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    EMPTY_QUERY_MESSAGE,
    ToolResult,
    ToolRouter,
)
from tests.fakes import FakeClock, FakeDevOpsAgent, FakeGuardrail, FakeSessionStore

_SESSION_ID: Final = "session-1"
"""Voice_Session identifier passed to every routed call."""

_ENGINEER_ID: Final = "engineer-1"
"""Engineer identity passed to every routed call."""

_TOOL_USE_ID: Final = "tool-use-1"
"""Fixed ``toolUseId``, echoed by the router into every tool result."""

_QUERY: Final = "list the unhealthy ALB targets"
"""Default engineer request text carried by the tool input."""

_ANSWER_CHUNKS: Final = ("The target group ", "has two unhealthy targets.")
"""Default scripted agent response chunks, accumulated in arrival order."""

_ANSWER: Final = "".join(_ANSWER_CHUNKS)
"""Concatenation of the default chunks: the expected accumulated answer."""

_FIRST_CHAT_ID: Final = "chat-1"
"""Deterministic id of the first chat a fresh ``FakeDevOpsAgent`` creates."""

_SECOND_CHAT_ID: Final = "chat-2"
"""Deterministic id of the second chat, created on mapping recovery."""

_TINY_TIMEOUT_SECONDS: Final = 0.05
"""Sub-second stream budget so timeout tests finish without real waiting."""

_PROMPT_RETURN_SECONDS: Final = 1.0
"""Wall-clock bound proving a timed-out call stopped consuming promptly."""

_AGENT_FAILED_EVENT: Final = "tool.agent_failed"
"""``event`` extra field of the router's agent-failure log entry (Req 3.7)."""


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
    records and the root logger configuration is never touched.

    Returns:
        The logger and its recording handler.
    """
    handler = _RecordingHandler()
    logger = logging.getLogger("tests.unit.tool_router_edges")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


@dataclass(frozen=True, slots=True)
class _Harness:
    """Assembled router under test with its fakes and captured logs.

    Attributes:
        router: The ``ToolRouter`` wired to the fakes below.
        guardrail: Default-pass guardrail fake recording evaluations.
        agent: Agent fake with deterministic chat ids and scripted chunks.
        store: In-memory session store holding the chat mapping.
        clock: Injected clock; never advanced, so TTL inputs are stable.
        handler: Recording handler capturing every router log record.
    """

    router: ToolRouter
    guardrail: FakeGuardrail
    agent: FakeDevOpsAgent
    store: FakeSessionStore
    clock: FakeClock
    handler: _RecordingHandler

    async def call(
        self,
        *,
        tool_input: str | Mapping[str, object] | None = None,
        execution_id: str | None = None,
    ) -> ToolResult:
        """Route one ``ask_devops_agent`` invocation with fixed identities.

        Args:
            tool_input: Tool input to route; defaults to a parsed mapping
                carrying the module's default query.
            execution_id: Execution scope of the session, or ``None`` for
                an unscoped session.

        Returns:
            The ``ToolResult`` produced by the router.
        """
        return await self.router.handle_tool_use(
            session_id=_SESSION_ID,
            engineer_id=_ENGINEER_ID,
            execution_id=execution_id,
            tool_use_id=_TOOL_USE_ID,
            tool_input=tool_input if tool_input is not None else {"query": _QUERY},
        )


def _make_harness(
    *,
    fail_create: bool = False,
    fail_send: bool = False,
    hang_forever: bool = False,
    agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
    chunks: Sequence[str] = _ANSWER_CHUNKS,
) -> _Harness:
    """Assemble a router over fresh fakes with the given failure script.

    Args:
        fail_create: Make the agent's ``create_chat`` raise (Req 3.7).
        fail_send: Make the agent's ``send_message`` raise on first
            iteration (Req 3.7).
        hang_forever: Make the agent suspend before its first chunk on an
            event that is never set (Req 3.8).
        agent_timeout_seconds: Stream budget passed to the router.
        chunks: Response chunks scripted globally on the agent.

    Returns:
        The assembled harness: router, fakes, clock, and log capture.
    """
    guardrail = FakeGuardrail()
    agent = FakeDevOpsAgent(
        fail_create=fail_create,
        fail_send=fail_send,
        hang_forever=hang_forever,
    )
    agent.respond_with(chunks)
    store = FakeSessionStore()
    clock = FakeClock()
    logger, handler = _make_logger()
    router = ToolRouter(
        guardrail,
        agent,
        store,
        agent_timeout_seconds=agent_timeout_seconds,
        clock=clock.now,
        logger=logger,
    )
    return _Harness(
        router=router,
        guardrail=guardrail,
        agent=agent,
        store=store,
        clock=clock,
        handler=handler,
    )


def _payload(result: ToolResult) -> dict[str, object]:
    """Parse a tool result's stringified JSON content.

    Args:
        result: The tool result whose content to parse.

    Returns:
        The deserialized content payload.

    Raises:
        AssertionError: If the content is not a JSON object.
    """
    parsed: object = json.loads(result.content)
    assert isinstance(parsed, dict)
    return parsed


def _failure_records(handler: _RecordingHandler) -> list[logging.LogRecord]:
    """Filter captured records down to agent-failure entries.

    Args:
        handler: The harness's recording handler.

    Returns:
        The records tagged ``tool.agent_failed``, in emission order.
    """
    return [
        record
        for record in handler.records
        if getattr(record, "event", None) == _AGENT_FAILED_EVENT
    ]


def _assert_failure_logged(handler: _RecordingHandler, exception_class: str) -> None:
    """Assert exactly one failure record naming the class and session.

    Args:
        handler: The harness's recording handler.
        exception_class: Expected ``exception_class`` extra field value
            (Req 3.7).

    Raises:
        AssertionError: If there is not exactly one agent-failure record,
            or it does not carry the expected exception class and the
            session identifier.
    """
    records = _failure_records(handler)
    assert len(records) == 1
    record = records[0]
    assert getattr(record, "exception_class", None) == exception_class
    assert getattr(record, "session_id", None) == _SESSION_ID
    assert record.exc_info is not None


async def test_first_call_creates_scoped_chat_and_persists_mapping() -> None:
    """First invocation creates the chat, persists it, and answers.

    The chat is created exactly once with the session's executionId
    passed through (Req 3.2, 3.5), the mapping is persisted with a TTL of
    the injected clock plus the 30-day retention period (Req 8.4), and
    the accumulated answer is returned as the tool result (Req 3.4).
    """
    harness = _make_harness()
    execution_id = "exec-42"

    result = await harness.call(execution_id=execution_id)

    assert result.ok
    assert result.tool_use_id == _TOOL_USE_ID
    assert _payload(result) == {"answer": _ANSWER}
    assert harness.agent.create_calls == [execution_id]
    assert harness.agent.send_calls == [(_FIRST_CHAT_ID, _QUERY)]
    assert harness.store.chat_ids == {_SESSION_ID: (_FIRST_CHAT_ID, execution_id)}
    expected_ttl = int(harness.clock.now()) + DEFAULT_RETENTION_DAYS * SECONDS_PER_DAY
    assert harness.store.ttls == [("put_chat_id", expected_ttl)]


async def test_second_call_reuses_persisted_chat() -> None:
    """A subsequent invocation reuses the persisted chat identifier.

    Across two calls of the same session the agent sees exactly one
    ``CreateChat`` and both requests travel on the same chat (Req 3.9).
    """
    harness = _make_harness()
    follow_up = "and the day before?"

    first = await harness.call()
    second = await harness.call(tool_input={"query": follow_up})

    assert first.ok
    assert second.ok
    assert harness.agent.create_calls == [None]
    assert harness.agent.send_calls == [
        (_FIRST_CHAT_ID, _QUERY),
        (_FIRST_CHAT_ID, follow_up),
    ]
    assert harness.store.chat_ids == {_SESSION_ID: (_FIRST_CHAT_ID, None)}


async def test_missing_mapping_recovery_creates_and_repersists() -> None:
    """A lost chat mapping is recovered with a fresh persisted chat.

    When the persisted mapping disappears between calls, the next call
    creates a new chat and persists the new identifier keyed by the same
    session (Req 3.10).
    """
    harness = _make_harness()

    first = await harness.call()
    harness.store.chat_ids.clear()
    second = await harness.call()

    assert first.ok
    assert second.ok
    assert harness.agent.create_calls == [None, None]
    assert harness.agent.send_calls == [
        (_FIRST_CHAT_ID, _QUERY),
        (_SECOND_CHAT_ID, _QUERY),
    ]
    assert harness.store.chat_ids == {_SESSION_ID: (_SECOND_CHAT_ID, None)}


async def test_create_chat_failure_returns_spoken_error_and_logs() -> None:
    """A failing ``CreateChat`` yields the spoken failure indication.

    The router returns the :data:`AGENT_FAILURE_MESSAGE` error tool
    result so Nova_Sonic informs the engineer verbally, logs the failure
    naming ``AgentRequestError`` and the session identifier (Req 3.7),
    and neither sends a message nor persists a mapping.
    """
    harness = _make_harness(fail_create=True)

    result = await harness.call()

    assert not result.ok
    assert _payload(result) == {"error": AGENT_FAILURE_MESSAGE}
    _assert_failure_logged(harness.handler, "AgentRequestError")
    assert harness.agent.send_calls == []
    assert harness.store.chat_ids == {}


async def test_send_message_failure_returns_error_and_keeps_mapping() -> None:
    """A failing ``SendMessage`` yields the spoken failure indication.

    The router returns the :data:`AGENT_FAILURE_MESSAGE` error tool
    result and logs ``AgentRequestError`` with the session identifier
    (Req 3.7); the chat mapping persisted after the successful
    ``CreateChat`` stays in the store for the next call to reuse.
    """
    harness = _make_harness(fail_send=True)

    result = await harness.call()

    assert not result.ok
    assert _payload(result) == {"error": AGENT_FAILURE_MESSAGE}
    _assert_failure_logged(harness.handler, "AgentRequestError")
    assert harness.agent.create_calls == [None]
    assert harness.store.chat_ids == {_SESSION_ID: (_FIRST_CHAT_ID, None)}


async def test_timeout_stops_stream_consumption_promptly() -> None:
    """An agent that never responds is cut off at the stream budget.

    With the agent suspended before its first chunk and a sub-second
    budget, the router returns the :data:`AGENT_TIMEOUT_MESSAGE` error
    tool result, logs ``AgentTimeoutError`` with the session identifier,
    and stops consuming the stream — the call returns promptly instead of
    waiting on the never-completing generator (Req 3.8).
    """
    harness = _make_harness(
        hang_forever=True, agent_timeout_seconds=_TINY_TIMEOUT_SECONDS
    )

    started = time.monotonic()
    result = await harness.call()
    elapsed = time.monotonic() - started

    assert elapsed < _PROMPT_RETURN_SECONDS
    assert not result.ok
    assert _payload(result) == {"error": AGENT_TIMEOUT_MESSAGE}
    _assert_failure_logged(harness.handler, "AgentTimeoutError")
    assert harness.agent.send_calls == [(_FIRST_CHAT_ID, _QUERY)]


async def test_mid_stream_timeout_discards_partial_chunks() -> None:
    """A stream stalling after its first chunk times out without leaking.

    The agent yields one chunk and then stalls on an event that is never
    set; at the budget the router returns the timeout error tool result
    and the already-received partial chunk appears nowhere in the result
    content (Req 3.8).
    """
    partial_chunk = "partial diagnostic answer"
    harness = _make_harness(
        agent_timeout_seconds=_TINY_TIMEOUT_SECONDS,
        chunks=(partial_chunk, "never delivered"),
    )
    never_set = asyncio.Event()
    delivered = 0

    async def stall_after_first_chunk() -> None:
        """Pass the first chunk through, then suspend forever.

        Raises:
            asyncio.CancelledError: When the router's stream budget
                expires and cancels the pending wait.
        """
        nonlocal delivered
        delivered += 1
        if delivered > 1:
            await never_set.wait()

    harness.agent.chunk_delay = stall_after_first_chunk

    result = await harness.call()

    assert not result.ok
    assert _payload(result) == {"error": AGENT_TIMEOUT_MESSAGE}
    assert partial_chunk not in result.content
    _assert_failure_logged(harness.handler, "AgentTimeoutError")


async def test_store_read_failure_returns_error_without_agent_call() -> None:
    """A failing chat-mapping read never reaches the DevOps_Agent.

    When ``get_chat_id`` raises, the router returns the
    :data:`AGENT_FAILURE_MESSAGE` error tool result, logs
    ``SessionStoreError`` with the session identifier (Req 8.5), and
    makes no ``CreateChat`` or ``SendMessage`` call.
    """
    harness = _make_harness()
    harness.store.fail_reads = True

    result = await harness.call()

    assert not result.ok
    assert _payload(result) == {"error": AGENT_FAILURE_MESSAGE}
    _assert_failure_logged(harness.handler, "SessionStoreError")
    assert harness.agent.create_calls == []
    assert harness.agent.send_calls == []


@pytest.mark.parametrize(
    "tool_input",
    [
        pytest.param({"query": ""}, id="empty-query-value"),
        pytest.param({"query": "   "}, id="blank-query-value"),
        pytest.param({}, id="missing-query-key"),
        pytest.param({"query": 7}, id="non-string-query-value"),
        pytest.param("", id="empty-raw-string"),
        pytest.param("   ", id="blank-raw-string"),
        pytest.param(json.dumps({"query": " "}), id="blank-json-string-query"),
    ],
)
async def test_unusable_query_short_circuits_before_guardrail(
    tool_input: str | Mapping[str, object],
) -> None:
    """A tool input carrying no usable query is refused before the gate.

    Empty, blank, missing, and non-string queries all yield the
    :data:`EMPTY_QUERY_MESSAGE` error tool result without any guardrail
    evaluation or agent call.

    Args:
        tool_input: A tool input from which no non-blank query text can
            be extracted.
    """
    harness = _make_harness()

    result = await harness.call(tool_input=tool_input)

    assert not result.ok
    assert _payload(result) == {"error": EMPTY_QUERY_MESSAGE}
    assert harness.guardrail.evaluations == []
    assert harness.agent.create_calls == []
    assert harness.agent.send_calls == []


async def test_mapping_tool_input_query_reaches_guardrail() -> None:
    """A parsed-mapping tool input's ``"query"`` value reaches the gate.

    The guardrail evaluates exactly the mapping's ``"query"`` string
    (Req 4.1) and the agent receives the same text — the parsed-mapping
    counterpart of the stringified-JSON and raw-string extraction cases.
    """
    harness = _make_harness()

    result = await harness.call(tool_input={"query": _QUERY})

    assert result.ok
    assert harness.guardrail.evaluations == [_QUERY]
    assert harness.agent.send_calls == [(_FIRST_CHAT_ID, _QUERY)]


async def test_json_string_tool_input_query_extracted() -> None:
    """A stringified-JSON tool input has its ``"query"`` value extracted.

    The guardrail and the agent both see the extracted query value, not
    the surrounding JSON envelope, and the call answers normally.
    """
    harness = _make_harness()
    query = "which lambda is failing"

    result = await harness.call(tool_input=json.dumps({"query": query}))

    assert result.ok
    assert _payload(result) == {"answer": _ANSWER}
    assert harness.guardrail.evaluations == [query]
    assert harness.agent.send_calls == [(_FIRST_CHAT_ID, query)]


async def test_non_json_string_tool_input_used_verbatim() -> None:
    """A non-JSON string tool input is treated as the whole query.

    The lenient extraction forwards the raw string verbatim, so the
    guardrail evaluates exactly what the model produced and the agent
    receives the same text.
    """
    harness = _make_harness()
    raw_text = "describe the queue backlog please"

    result = await harness.call(tool_input=raw_text)

    assert result.ok
    assert harness.guardrail.evaluations == [raw_text]
    assert harness.agent.send_calls == [(_FIRST_CHAT_ID, raw_text)]
