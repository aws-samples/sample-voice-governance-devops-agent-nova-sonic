# Feature: nova-sonic-support-portal, Property 5: Chat identifier is created once and reused
"""Property test: one DevOps_Agent chat per Voice_Session, created once, reused.

**Validates: Requirements 3.2, 3.9**

For any number n ≥ 1 of ``ask_devops_agent`` invocations within one
Voice_Session — with a Session_Store that retains the mapping — the tool
router calls ``create_chat`` exactly once, on the first invocation, and
persists the returned chat identifier keyed by the Voice_Session exactly
once (Req 3.2); every one of the n ``send_message`` calls carries that
same persisted chat identifier, in invocation order, so conversation
continuity with the DevOps_Agent is preserved across the whole session
(Req 3.9, design Property 5). Each invocation completes the full path:
its tool result echoes the invocation's ``toolUseId`` and carries the
agent's accumulated answer.

Each example draws one session — identifier, engineer identity, and an
optional execution scope held constant across the session (what scoping
value ``create_chat`` receives is Property 6's concern, not asserted
here) — plus a sequence of n ≥ 1 non-blank queries, and drives
``ToolRouter.handle_tool_use`` once per query against fresh fakes: a
:class:`FakeGuardrail` passing every query (gate outcomes are Property
7's concern), a :class:`FakeDevOpsAgent` scripted to answer every
forwarded request with deterministic chat identifiers ``chat-1``,
``chat-2``, ..., an initially empty :class:`FakeSessionStore` that
retains every write, and an injected :class:`FakeClock`, so creation
count, persistence count, and the chat identifier of every
``send_message`` call are all observable and deterministic.
``hypothesis.given`` cannot drive ``async def`` tests under
pytest-asyncio, so each example runs its coroutine to completion with
``asyncio.run`` from a synchronous test body — the same pattern as the
Property 7 suite, giving every example a fresh event loop.
"""

import asyncio
import json
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.mutation_guard import has_mutation_intent
from app.orchestration.tool_router import ToolResult, ToolRouter
from tests.fakes import FakeClock, FakeDevOpsAgent, FakeGuardrail, FakeSessionStore

_ANSWER_TEXT: Final = "answer"
"""Scripted agent response chunk returned for every forwarded request."""

_FIRST_CHAT_ID: Final = "chat-1"
"""Deterministic id of the first chat a fresh ``FakeDevOpsAgent`` creates."""

_MAX_INVOCATIONS: Final = 8
"""Upper bound on n, the number of invocations driven through one session."""

_QUERIES: Final = st.lists(
    # Non-blank, and free of mutation instructions: this property is about
    # chat identity, so every query must reach the agent. Requests that
    # instruct a state change are refused before any agent call by the
    # deterministic backstop (app.domain.mutation_guard), which arbitrary
    # generated text occasionally trips ("TERMINATING").
    st.text(min_size=1).filter(str.strip).filter(lambda q: not has_mutation_intent(q)),
    min_size=1,
    max_size=_MAX_INVOCATIONS,
)
"""Strategy for one session's request texts: n ≥ 1 non-blank queries in order."""

_EXECUTION_IDS: Final = st.one_of(st.none(), st.text(min_size=1))
"""Strategy for the session's fixed origin: an executionId, or none (Req 3.5, 3.6)."""


def _tool_use_id(index: int) -> str:
    """Return the deterministic ``toolUseId`` of one invocation.

    Args:
        index: Zero-based position of the invocation within the session.

    Returns:
        A ``toolUseId`` unique within the session, echoed by the router
        into the invocation's tool result.
    """
    return f"tool-use-p05-{index}"


async def _check_chat_reuse(
    queries: list[str],
    session_id: str,
    engineer_id: str,
    execution_id: str | None,
) -> None:
    """Drive n invocations through one session and assert single-create reuse.

    Args:
        queries: The session's n ≥ 1 non-blank request texts, one per
            ``ask_devops_agent`` invocation, in order.
        session_id: Voice_Session identifier keying the chat mapping.
        engineer_id: Engineer identity passed to the router.
        execution_id: The session's fixed execution scope, passed
            identically on every invocation.

    Raises:
        AssertionError: If ``create_chat`` is not called exactly once,
            the mapping is not persisted exactly once, any
            ``send_message`` call carries something other than the
            persisted chat identifier, or any invocation fails to
            return the agent's answer.
    """
    guardrail = FakeGuardrail()
    agent = FakeDevOpsAgent()
    agent.respond_with([_ANSWER_TEXT])
    store = FakeSessionStore()
    router = ToolRouter(guardrail, agent, store, clock=FakeClock().now)

    results: list[ToolResult] = []
    for index, query in enumerate(queries):
        result = await router.handle_tool_use(
            session_id=session_id,
            engineer_id=engineer_id,
            execution_id=execution_id,
            tool_use_id=_tool_use_id(index),
            tool_input={"query": query},
        )
        results.append(result)

    # create_chat exactly once, on the first invocation (Req 3.2).
    assert len(agent.create_calls) == 1
    # The created identifier is persisted keyed by the session (Req 3.2),
    # by exactly one mapping write.
    persisted = store.chat_ids.get(session_id)
    assert persisted is not None
    chat_id = persisted[0]
    assert chat_id == _FIRST_CHAT_ID
    put_calls = [call for call in store.calls if call[0] == "put_chat_id"]
    assert len(put_calls) == 1
    # All n send_message calls carry that same persisted chat identifier,
    # in invocation order (Req 3.9).
    assert agent.send_calls == [(chat_id, query) for query in queries]
    # Every invocation completed the full path with the agent's answer.
    for index, result in enumerate(results):
        assert result.tool_use_id == _tool_use_id(index)
        assert result.ok
        assert json.loads(result.content) == {"answer": _ANSWER_TEXT}


@given(
    queries=_QUERIES,
    session_id=st.text(min_size=1),
    engineer_id=st.text(min_size=1),
    execution_id=_EXECUTION_IDS,
)
@settings(max_examples=100, deadline=None)
def test_chat_identifier_created_once_and_reused(
    queries: list[str],
    session_id: str,
    engineer_id: str,
    execution_id: str | None,
) -> None:
    """The session's chat is created and persisted once; all n calls reuse it.

    For any n ≥ 1 invocations within one Voice_Session whose store
    retains the mapping: ``create_chat`` is called exactly once, the
    returned chat identifier is persisted keyed by the Voice_Session by
    exactly one mapping write, and every ``send_message`` call of the
    session carries that same persisted identifier in invocation order,
    each invocation returning the agent's answer as its tool result.

    Args:
        queries: The session's n ≥ 1 non-blank request texts, in order.
        session_id: Voice_Session identifier drawn from arbitrary text.
        engineer_id: Engineer identity drawn from arbitrary text.
        execution_id: The session's fixed execution scope: an arbitrary
            executionId or ``None``, constant across the session.
    """
    asyncio.run(_check_chat_reuse(queries, session_id, engineer_id, execution_id))
