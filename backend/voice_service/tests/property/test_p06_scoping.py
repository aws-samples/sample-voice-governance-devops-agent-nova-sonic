# Feature: nova-sonic-support-portal, Property 6: Execution scoping follows the session's origin
"""Property test: execution scoping follows the session's origin.

**Validates: Requirements 3.5, 3.6**

For any Voice_Session origin — opened from an Incident_Notification
carrying an executionId, or opened without one — the tool router's
first ``ask_devops_agent`` invocation creates the DevOps_Agent chat
with a scoping argument that matches the origin exactly: ``CreateChat``
receives precisely the session's executionId when the session is
incident-scoped (Req 3.5) and ``None`` — no execution scoping — when it
is not (Req 3.6), and the chat mapping persisted to the Session_Store
preserves that same scoping alongside the created chat id (design
Property 6).

Each example draws an origin (arbitrary executionId text or ``None``)
together with arbitrary query, session-identifier, and engineer-identity
text, and drives ``ToolRouter.handle_tool_use`` end to end against
fresh fakes: a pass-everything :class:`FakeGuardrail` (so the gate never
interferes with chat creation), a :class:`FakeDevOpsAgent` scripted to
answer the forwarded request and recording every ``create_chat``
argument, an empty :class:`FakeSessionStore`, and an injected
:class:`FakeClock`. ``hypothesis.given`` cannot drive ``async def``
tests under pytest-asyncio, so each example runs its coroutine to
completion with ``asyncio.run`` from a synchronous test body — the same
pattern as the Property 7 suite, giving every example a fresh event
loop.
"""

import asyncio
import json
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.orchestration.tool_router import ToolRouter
from tests.fakes import FakeClock, FakeDevOpsAgent, FakeGuardrail, FakeSessionStore

_ANSWER_TEXT: Final = "answer"
"""Scripted agent response chunk returned for the forwarded request."""

_TOOL_USE_ID: Final = "tool-use-p06"
"""Fixed ``toolUseId``, echoed by the router into every tool result."""

_FIRST_CHAT_ID: Final = "chat-1"
"""Deterministic id of the first chat a fresh ``FakeDevOpsAgent`` creates."""

_EXECUTION_IDS: Final = st.one_of(st.none(), st.text(min_size=1))
"""Session-origin strategy: an incident executionId, or ``None`` when unscoped."""


async def _check_scoping(
    execution_id: str | None,
    query: str,
    session_id: str,
    engineer_id: str,
) -> None:
    """Drive one tool invocation and assert the chat scoping matches the origin.

    Args:
        execution_id: The session's origin: the executionId the
            Voice_Session was opened from, or ``None`` for a session
            opened without incident scoping.
        query: Non-blank engineer request text carried by the tool input.
        session_id: Voice_Session identifier passed to the router.
        engineer_id: Engineer identity passed to the router.

    Raises:
        AssertionError: If the invocation does not complete with the
            scripted answer, if ``CreateChat`` is not called exactly once
            with exactly the session's executionId (``None`` for an
            unscoped session), or if the persisted chat mapping does not
            preserve that scoping.
    """
    guardrail = FakeGuardrail()
    agent = FakeDevOpsAgent()
    agent.respond_with([_ANSWER_TEXT])
    store = FakeSessionStore()
    router = ToolRouter(guardrail, agent, store, clock=FakeClock().now)

    result = await router.handle_tool_use(
        session_id=session_id,
        engineer_id=engineer_id,
        execution_id=execution_id,
        tool_use_id=_TOOL_USE_ID,
        tool_input={"query": query},
    )

    assert result.ok
    assert result.tool_use_id == _TOOL_USE_ID
    assert json.loads(result.content) == {"answer": _ANSWER_TEXT}
    assert agent.create_calls == [execution_id]
    assert agent.send_calls == [(_FIRST_CHAT_ID, query)]
    assert store.chat_ids[session_id] == (_FIRST_CHAT_ID, execution_id)


@given(
    execution_id=_EXECUTION_IDS,
    query=st.text(min_size=1).filter(str.strip),
    session_id=st.text(min_size=1),
    engineer_id=st.text(min_size=1),
)
@settings(max_examples=100, deadline=None)
def test_execution_scoping_follows_session_origin(
    execution_id: str | None,
    query: str,
    session_id: str,
    engineer_id: str,
) -> None:
    """The created chat's execution scoping equals the session's origin exactly.

    For every origin: when the session carries an executionId, the single
    ``CreateChat`` call is scoped to exactly that executionId (Req 3.5);
    when it carries none, the call is made with no execution scoping
    (Req 3.6); and the Session_Store mapping records the created chat id
    together with that same scoping.

    Args:
        execution_id: Session origin drawn from arbitrary executionId
            text or ``None``.
        query: Non-blank engineer request text carried by the tool input.
        session_id: Voice_Session identifier drawn from arbitrary text.
        engineer_id: Engineer identity drawn from arbitrary text.
    """
    asyncio.run(_check_scoping(execution_id, query, session_id, engineer_id))
