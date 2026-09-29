# Feature: nova-sonic-support-portal, Property 7: Guardrail gate is fail-closed
"""Property test: the tool router's guardrail gate is fail-closed.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7**

For any guardrail evaluation outcome — pass, intervention, any Automated
Reasoning finding type (valid, invalid, satisfiable, impossible,
translation-ambiguous, untranslatable), SDK exception, timeout, or
malformed response — the tool router forwards the request to the
DevOps_Agent if and only if the outcome is an explicit pass with no
non-compliant finding; in every other case it returns a refusal tool
result carrying the canonical
:data:`app.domain.guardrail_policy.REFUSAL_MESSAGE` (stating only read
and diagnostic operations are supported) without any DevOps_Agent call,
and emits exactly one audit record tagged ``guardrail.blocked`` carrying
the blocked content, the Voice_Session identifier, the engineer identity,
and a parseable ISO-8601 timestamp (design Property 7).

The outcome strategy spans the full space of the design's Guardrail
Evaluation Model, one branch per outcome class:

- **pass** — ``action == "NONE"`` with zero to five VALID findings (the
  single class allowed to forward);
- **intervention** — ``action == "GUARDRAIL_INTERVENED"`` with arbitrary
  findings (Req 4.2);
- **non-compliant finding** — ``action == "NONE"`` with at least one
  recognized blocking finding type (INVALID, SATISFIABLE, IMPOSSIBLE,
  TRANSLATION_AMBIGUOUS, NO_TRANSLATION) shuffled among VALID findings;
- **malformed** — a missing or unrecognized ``action``, or ``action ==
  "NONE"`` with uninterpretable findings (missing or unrecognized result
  types) shuffled among VALIDs (Req 4.7);
- **unavailable** — the guardrail raises ``GuardrailUnavailableError``
  with an arbitrary reason, the normalized shape of SDK exceptions,
  timeouts, and throttles (Req 4.6).

Each example scripts one outcome into a fresh :class:`FakeGuardrail` and
drives ``ToolRouter.handle_tool_use`` end to end against fresh fakes: a
:class:`FakeDevOpsAgent` scripted to answer any forwarded request, an
empty :class:`FakeSessionStore`, an injected :class:`FakeClock`, and a
private capturing logger, so forwarding, refusal content, and the audit
trail are all observable and deterministic. ``hypothesis.given`` cannot
drive ``async def`` tests under pytest-asyncio, so each example runs its
coroutine to completion with ``asyncio.run`` from a synchronous test body
— the same pattern as the Property 14 suite, giving every example a
fresh event loop.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.guardrail_policy import (
    ACTION_GUARDRAIL_INTERVENED,
    ACTION_NONE,
    BLOCKING_FINDINGS,
    FINDING_VALID,
    REFUSAL_MESSAGE,
    Finding,
    GuardrailEvaluation,
)
from app.orchestration.tool_router import ToolRouter
from tests.fakes import FakeClock, FakeDevOpsAgent, FakeGuardrail, FakeSessionStore

_AUDIT_BLOCKED_EVENT: Final = "guardrail.blocked"
"""``event`` extra field of the router's guardrail audit log entry (Req 4.5)."""

_ANSWER_TEXT: Final = "answer"
"""Scripted agent response chunk returned for every forwarded request."""

_TOOL_USE_ID: Final = "tool-use-p07"
"""Fixed ``toolUseId``, echoed by the router into every tool result."""

_FIRST_CHAT_ID: Final = "chat-1"
"""Deterministic id of the first chat a fresh ``FakeDevOpsAgent`` creates."""


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
    replaced, propagation disabled — so each hypothesis example observes
    only its own records and the root logger configuration is never
    touched.

    Returns:
        The logger and its recording handler.
    """
    handler = _RecordingHandler()
    logger = logging.getLogger("tests.property.p07")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


def _events(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    """Filter captured records down to those tagged with ``event``.

    Args:
        records: All records captured by the recording handler, in order.
        event: Expected value of the record's ``event`` extra field.

    Returns:
        The records whose ``event`` extra equals ``event``, in order.
    """
    return [record for record in records if getattr(record, "event", None) == event]


@dataclass(frozen=True, slots=True)
class _ScriptedOutcome:
    """One guardrail outcome to script, plus the gate result it must produce.

    Attributes:
        expect_pass: Whether the outcome is the explicit-pass class — the
            only class on which the router may forward to the DevOps_Agent.
        evaluation: Evaluation for ``FakeGuardrail.queue_evaluation``, or
            ``None`` when the outcome is the unavailable class.
        unavailable_reason: Reason for ``FakeGuardrail.raise_unavailable``
            — the SDK-exception / timeout / throttle class (Req 4.6) — or
            ``None`` when the guardrail returns ``evaluation`` instead.
    """

    expect_pass: bool
    evaluation: GuardrailEvaluation | None = None
    unavailable_reason: str | None = None


_VALID_FINDING: Final = st.just(Finding(result=FINDING_VALID))
"""Strategy for the single Automated Reasoning finding type compatible with PASS."""

_BLOCKING_FINDING: Final = st.sampled_from(sorted(BLOCKING_FINDINGS)).map(
    lambda result: Finding(result=result)
)
"""Strategy over every recognized non-VALID finding type, each of which blocks."""

_MALFORMED_FINDING: Final = st.one_of(
    st.none(),
    st.text().filter(
        lambda result: result != FINDING_VALID and result not in BLOCKING_FINDINGS
    ),
).map(lambda result: Finding(result=result))
"""Strategy for uninterpretable findings: missing or unrecognized result types."""

_ANY_FINDING: Final = st.one_of(_VALID_FINDING, _BLOCKING_FINDING, _MALFORMED_FINDING)
"""Strategy over the full finding space, for classes that block regardless."""

_UNKNOWN_ACTION: Final = st.one_of(
    st.none(),
    st.text().filter(
        lambda action: action not in (ACTION_NONE, ACTION_GUARDRAIL_INTERVENED)
    ),
)
"""Strategy for malformed ``action`` values: missing or unrecognized strings."""

_PASS_EVALUATIONS: Final = st.lists(_VALID_FINDING, max_size=5).map(
    lambda findings: GuardrailEvaluation(action=ACTION_NONE, findings=tuple(findings))
)
"""Explicit-pass class: ``action == "NONE"`` with zero to five VALID findings."""

_INTERVENED_EVALUATIONS: Final = st.lists(_ANY_FINDING, max_size=5).map(
    lambda findings: GuardrailEvaluation(
        action=ACTION_GUARDRAIL_INTERVENED, findings=tuple(findings)
    )
)
"""Intervention class: ``GUARDRAIL_INTERVENED`` with arbitrary findings (Req 4.2)."""


@st.composite
def _blocking_finding_evaluations(draw: st.DrawFn) -> GuardrailEvaluation:
    """Draw a non-compliant-finding evaluation that must block.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        A ``GuardrailEvaluation`` with ``action == "NONE"`` whose findings
        mix at least one recognized non-VALID finding type with zero or
        more VALID findings, in shuffled order.
    """
    blocking = draw(st.lists(_BLOCKING_FINDING, min_size=1, max_size=3))
    valid = draw(st.lists(_VALID_FINDING, max_size=4))
    findings = draw(st.permutations(blocking + valid))
    return GuardrailEvaluation(action=ACTION_NONE, findings=tuple(findings))


@st.composite
def _malformed_evaluations(draw: st.DrawFn) -> GuardrailEvaluation:
    """Draw a malformed evaluation that must block (Req 4.7).

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        A ``GuardrailEvaluation`` malformed in one of the two ways the
        router can observe: an ``action`` that is missing or not a
        recognized value (with arbitrary findings), or ``action == "NONE"``
        with at least one uninterpretable finding shuffled among VALID
        findings.
    """
    if draw(st.booleans()):
        action = draw(_UNKNOWN_ACTION)
        findings = draw(st.lists(_ANY_FINDING, max_size=5))
    else:
        action = ACTION_NONE
        malformed = draw(st.lists(_MALFORMED_FINDING, min_size=1, max_size=3))
        valid = draw(st.lists(_VALID_FINDING, max_size=4))
        findings = draw(st.permutations(malformed + valid))
    return GuardrailEvaluation(action=action, findings=tuple(findings))


_OUTCOMES: Final = st.one_of(
    _PASS_EVALUATIONS.map(
        lambda evaluation: _ScriptedOutcome(expect_pass=True, evaluation=evaluation)
    ),
    _INTERVENED_EVALUATIONS.map(
        lambda evaluation: _ScriptedOutcome(expect_pass=False, evaluation=evaluation)
    ),
    _blocking_finding_evaluations().map(
        lambda evaluation: _ScriptedOutcome(expect_pass=False, evaluation=evaluation)
    ),
    _malformed_evaluations().map(
        lambda evaluation: _ScriptedOutcome(expect_pass=False, evaluation=evaluation)
    ),
    st.text().map(
        lambda reason: _ScriptedOutcome(expect_pass=False, unavailable_reason=reason)
    ),
)
"""Full outcome space: pass, intervention, blocking findings, malformed, unavailable."""


async def _check_gate(
    outcome: _ScriptedOutcome,
    query: str,
    session_id: str,
    engineer_id: str,
) -> None:
    """Drive one scripted outcome through the router and assert the gate contract.

    Args:
        outcome: The guardrail outcome to script and its expected gate
            result.
        query: Non-blank engineer request text carried by the tool input.
        session_id: Voice_Session identifier passed to the router.
        engineer_id: Engineer identity passed to the router.

    Raises:
        AssertionError: If the router forwards on a non-pass outcome, fails
            to forward on a pass, returns the wrong tool result, or emits a
            missing or incomplete audit record.
    """
    guardrail = FakeGuardrail()
    if outcome.unavailable_reason is not None:
        guardrail.raise_unavailable(outcome.unavailable_reason)
    elif outcome.evaluation is not None:
        guardrail.queue_evaluation(outcome.evaluation)
    agent = FakeDevOpsAgent()
    agent.respond_with([_ANSWER_TEXT])
    store = FakeSessionStore()
    logger, handler = _make_logger()
    router = ToolRouter(guardrail, agent, store, clock=FakeClock().now, logger=logger)

    result = await router.handle_tool_use(
        session_id=session_id,
        engineer_id=engineer_id,
        execution_id=None,
        tool_use_id=_TOOL_USE_ID,
        tool_input={"query": query},
    )

    assert guardrail.evaluations == [query]
    assert result.tool_use_id == _TOOL_USE_ID
    payload = json.loads(result.content)
    blocked = _events(handler.records, _AUDIT_BLOCKED_EVENT)

    forwarded = agent.send_calls != []
    assert forwarded == outcome.expect_pass

    if outcome.expect_pass:
        assert result.ok
        assert payload == {"answer": _ANSWER_TEXT}
        assert agent.send_calls == [(_FIRST_CHAT_ID, query)]
        assert blocked == []
    else:
        assert not result.ok
        assert payload == {"error": REFUSAL_MESSAGE}
        assert agent.create_calls == []
        assert agent.send_calls == []
        assert len(blocked) == 1
        record = blocked[0]
        assert getattr(record, "blocked_content", None) == query
        assert getattr(record, "session_id", None) == session_id
        assert getattr(record, "engineer_id", None) == engineer_id
        timestamp = getattr(record, "timestamp", None)
        assert isinstance(timestamp, str)
        parsed = datetime.fromisoformat(timestamp)
        assert parsed.tzinfo is not None


@given(
    outcome=_OUTCOMES,
    query=st.text(min_size=1).filter(str.strip),
    session_id=st.text(min_size=1),
    engineer_id=st.text(min_size=1),
)
@settings(max_examples=100, deadline=None)
def test_guardrail_gate_is_fail_closed(
    outcome: _ScriptedOutcome,
    query: str,
    session_id: str,
    engineer_id: str,
) -> None:
    """The router forwards iff explicit pass; otherwise it refuses and audits.

    For every guardrail outcome: the DevOps_Agent receives the request if
    and only if the outcome is the explicit-pass class (``action ==
    "NONE"`` with every finding VALID). On every other outcome —
    intervention, any non-VALID finding, a malformed response, or an
    unavailable guardrail — the router returns the canonical refusal as an
    error tool result, makes no ``CreateChat`` or ``SendMessage`` call,
    and emits exactly one ``guardrail.blocked`` audit record carrying the
    blocked content, the session identifier, the engineer identity, and a
    parseable ISO-8601 timestamp.

    Args:
        outcome: Guardrail outcome drawn from the full evaluation space.
        query: Non-blank engineer request text carried by the tool input.
        session_id: Voice_Session identifier drawn from arbitrary text.
        engineer_id: Engineer identity drawn from arbitrary text.
    """
    asyncio.run(_check_gate(outcome, query, session_id, engineer_id))
