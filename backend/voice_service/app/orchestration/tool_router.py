"""Tool router: guardrail-gated ``ask_devops_agent`` dispatch (Req 3, 4).

Handles one ``toolUse(ask_devops_agent)`` event from the Nova Sonic
stream end to end (design component ``tool_router``):

1. **Query extraction** — the tool input arrives as the raw ``toolUse``
   input: stringified JSON per the tool spec, or an already-parsed
   mapping. Extraction is lenient: a mapping's string ``"query"`` value
   is used when present; a string is JSON-parsed and its ``"query"``
   value taken when that yields one, otherwise the whole string is the
   query. A blank query short-circuits to an error tool result without
   calling the guardrail.
1a. **Mutation backstop** (Req 4.2) — a request that unambiguously
   instructs a state change is refused deterministically, without a
   guardrail call, because the semantic topic lets some imperative
   mutations through (:mod:`app.domain.mutation_guard`). It only adds
   blocks; anything it declines still faces the full guardrail gate.
2. **Guardrail gate** (Req 4.1, 4.4) — every extracted query is
   evaluated through ``GuardrailPort``, and the evaluation — or the
   ``GuardrailUnavailableError`` raised in its place (Req 4.6) — is fed
   to the pure fail-closed decision function
   ``domain.guardrail_policy.decide``. On BLOCK the router returns the
   canonical ``REFUSAL_MESSAGE`` as an error tool result, so Nova_Sonic
   verbally informs the engineer that only read and diagnostic
   operations are supported (Req 4.3), without any DevOps_Agent call
   (Req 4.4, design Property 7), and writes an audit log entry carrying
   the blocked content, the Voice_Session identifier, the engineer
   identity, and a timestamp (Req 4.5).
3. **Chat resolution** (Req 3.2, 3.9, 3.10) — on PASS the session's
   chat identifier is read from the Session_Store; when absent (first
   invocation, or a lost mapping) a chat is created via
   ``DevOpsAgentPort.create_chat`` — scoped to the session's
   executionId when the session was opened from an incident, unscoped
   otherwise (Req 3.5, 3.6) — and persisted with a TTL computed by
   ``domain.ttl`` from the injected clock (Req 8.4).
4. **Ask the agent** (Req 3.3, 3.8) — the query is sent via
   ``DevOpsAgentPort.send_message`` and the streamed chunks are
   accumulated in arrival order (``domain.transcript.ChunkAccumulator``,
   design Property 4) under ``asyncio.timeout`` with the configured
   budget (60 seconds by design). The complete accumulated answer is
   returned as the tool result content ``{"answer": ...}`` (Req 3.4).

Expected failures never escape as exceptions: an agent request failure,
a stream-budget timeout, or a Session_Store failure is logged with the
exception class name and the Voice_Session identifier (Req 3.7, 3.8)
and converted into an error tool result ``{"error": ...}`` whose
message Nova_Sonic can verbalize. Successful answer content is never
logged; only guardrail-blocked content is recorded, as the audit trail
requires (Req 4.5).

Tool-result messages are module-level constants so tests and the
session manager reference the exact spoken wording. Time is injected as
an epoch-seconds callable (defaulting to ``time.time``) so TTL values
and audit timestamps are deterministic under test. This module reaches
AWS only through its ports and imports no SDK (Req 17.6).
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.domain.guardrail_policy import (
    REFUSAL_MESSAGE,
    Decision,
    DecisionOutcome,
    decide,
)
from app.domain.mutation_guard import MUTATION_INTENT_REASON, has_mutation_intent
from app.domain.transcript import ChunkAccumulator
from app.domain.ttl import DEFAULT_RETENTION_DAYS, compute_ttl_from_epoch
from app.exceptions import (
    AgentRequestError,
    AgentTimeoutError,
    DevOpsAgentError,
    GuardrailUnavailableError,
    SessionStoreError,
)
from app.ports.devops_agent import DevOpsAgentPort
from app.ports.guardrail import GuardrailPort
from app.ports.session_store import SessionStorePort

__all__ = [
    "AGENT_FAILURE_MESSAGE",
    "AGENT_TIMEOUT_MESSAGE",
    "DEFAULT_AGENT_TIMEOUT_SECONDS",
    "EMPTY_ANSWER_MESSAGE",
    "EMPTY_QUERY_MESSAGE",
    "ToolResult",
    "ToolRouter",
]

DEFAULT_AGENT_TIMEOUT_SECONDS: Final = 60.0
"""Design-default budget for consuming the agent's streamed response (Req 3.8)."""

AGENT_FAILURE_MESSAGE: Final = "The DevOps Agent request could not be completed."
"""Error tool-result message for a failed agent or store call, spoken by Nova_Sonic (Req 3.7)."""

AGENT_TIMEOUT_MESSAGE: Final = "The DevOps Agent response timed out."
"""Error tool-result message for an exhausted stream budget, spoken by Nova_Sonic (Req 3.8)."""

AGENT_BUSY_MESSAGE: Final = (
    "The DevOps Agent is rate limited right now and refused the request. "
    "Wait a few seconds before asking again; do not retry immediately."
)
"""Tool result for a throttled or quota-exceeded agent call.

Distinct from :data:`AGENT_FAILURE_MESSAGE` so the spoken outcome tells the
engineer to wait rather than implying the portal is broken, and so
Nova_Sonic is instructed not to retry into the same quota wall.
"""

EMPTY_QUERY_MESSAGE: Final = "The request contained no query text for the DevOps Agent."

EMPTY_ANSWER_MESSAGE: Final = (
    "The DevOps Agent completed the request but returned no content."
)
"""Tool result when the agent's answer accumulates to nothing.

Reported as an error rather than an empty answer so the outcome is
explicit: an empty ``{"answer": ""}`` left Nova_Sonic improvising around
silence ("I didn't get any results back"), indistinguishable from a real
"nothing found" and invisible in the logs, which is how a text-extraction
defect stayed hidden through several deployments.
"""
"""Error tool-result message for a tool input carrying no usable query."""

_QUERY_KEY: Final = "query"
"""Tool-input field holding the engineer request, per the ``ask_devops_agent`` spec."""

_AUDIT_BLOCKED_EVENT: Final = "guardrail.blocked"
"""``event`` field value of the guardrail audit log entry (Req 4.5)."""

_EMPTY_ANSWER_EVENT: Final = "tool.empty_answer"
"""``event`` log field value when the agent's answer accumulated to nothing."""

_AGENT_FAILED_EVENT: Final = "tool.agent_failed"
"""``event`` field value of the agent-failure log entry (Req 3.7)."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Immutable outcome of one ``ask_devops_agent`` tool invocation.

    The session manager feeds this to
    ``BedrockStreamPort.send_tool_result`` so Nova_Sonic speaks the
    answer — or the error indication — to the engineer (Req 3.4, 3.7).

    Attributes:
        tool_use_id: The ``toolUseId`` of the ``toolUse`` event this
            result answers.
        content: Stringified JSON tool-result payload: ``{"answer": ...}``
            on success, ``{"error": ...}`` on refusal or failure.
        ok: ``True`` when ``content`` carries an agent answer; ``False``
            when it carries a refusal or error indication.
    """

    tool_use_id: str
    content: str
    ok: bool


def _answer_result(tool_use_id: str, answer: str) -> ToolResult:
    """Build the success tool result carrying the agent's answer.

    Args:
        tool_use_id: The ``toolUseId`` the result answers.
        answer: The complete accumulated agent response text (Req 3.4).

    Returns:
        A ``ToolResult`` whose content is the stringified JSON
        ``{"answer": <answer>}`` with ``ok=True``.
    """
    return ToolResult(
        tool_use_id=tool_use_id,
        content=json.dumps({"answer": answer}),
        ok=True,
    )


def _error_result(tool_use_id: str, message: str) -> ToolResult:
    """Build an error tool result carrying a spoken error indication.

    Args:
        tool_use_id: The ``toolUseId`` the result answers.
        message: The refusal or error message Nova_Sonic verbalizes
            (Req 3.7, 4.3).

    Returns:
        A ``ToolResult`` whose content is the stringified JSON
        ``{"error": <message>}`` with ``ok=False``.
    """
    return ToolResult(
        tool_use_id=tool_use_id,
        content=json.dumps({"error": message}),
        ok=False,
    )


def _query_from_text(text: str) -> str:
    """Extract the query from a raw string tool input, leniently.

    Args:
        text: The raw ``toolUse`` input string — stringified JSON per
            the tool spec on a well-formed event, arbitrary text
            otherwise.

    Returns:
        The parsed mapping's string ``"query"`` value when ``text`` is
        JSON for a mapping carrying one; the whole string otherwise (a
        lenient fallback so imperfect model output still reaches the
        guardrail gate rather than being dropped).
    """
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, Mapping):
        candidate = parsed.get(_QUERY_KEY)
        if isinstance(candidate, str):
            return candidate
    return text


def _extract_query(tool_input: str | Mapping[str, object]) -> str:
    """Extract the engineer's query text from a ``toolUse`` input.

    Args:
        tool_input: The tool input as delivered by the stream consumer:
            an already-parsed mapping, or the raw input string.

    Returns:
        The query text: a mapping's string ``"query"`` value (empty
        when missing or not a string), or the extraction of
        :func:`_query_from_text` for a raw string.
    """
    if isinstance(tool_input, Mapping):
        candidate = tool_input.get(_QUERY_KEY)
        return candidate if isinstance(candidate, str) else ""
    return _query_from_text(tool_input)


def _iso_utc(epoch_seconds: float) -> str:
    """Render an epoch-seconds instant as an ISO-8601 UTC string.

    Args:
        epoch_seconds: The instant to render, in epoch seconds.

    Returns:
        The instant as an ISO-8601 UTC timestamp with a ``Z`` suffix,
        for example ``2024-01-01T00:00:00Z`` — the shape used across
        the domain modules for injected timestamps.
    """
    rendered = datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()
    return rendered.replace("+00:00", "Z")


class ToolRouter:
    """Routes ``ask_devops_agent`` tool calls through the guardrail gate.

    One instance serves every Voice_Session of the process: per-call
    state (query, chat identifier, accumulated answer) lives in local
    scope and the session identity travels as explicit arguments, so
    concurrent :meth:`handle_tool_use` calls never interfere. External
    reach is confined to the three injected ports (Req 17.6): the
    guardrail for the fail-closed gate (Req 4.1), the agent for chat
    creation and streamed answers (Req 3.2, 3.3), and the session store
    for the chat mapping (Req 3.9, 3.10).
    """

    def __init__(
        self,
        guardrail: GuardrailPort,
        agent: DevOpsAgentPort,
        store: SessionStorePort,
        *,
        agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        clock: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the router with its ports and policies.

        Args:
            guardrail: Port evaluating every query before it may reach
                the DevOps_Agent (Req 4.1).
            agent: Port for ``CreateChat`` and ``SendMessage``
                (Req 3.2, 3.3).
            store: Port persisting the session-to-chat mapping
                (Req 3.2, 3.9, 3.10).
            agent_timeout_seconds: Budget in seconds for consuming the
                agent's streamed response; defaults to the design's 60
                seconds (Req 3.8).
            retention_days: Retention period in days for the persisted
                chat mapping's TTL (Req 8.4).
            clock: Callable returning the current time in epoch
                seconds, used for TTL computation and audit timestamps;
                defaults to ``time.time``. Tests inject a fake clock
                for determinism.
            logger: Logger receiving audit and failure entries;
                defaults to this module's logger.
        """
        self._guardrail = guardrail
        self._agent = agent
        self._store = store
        self._agent_timeout_seconds = agent_timeout_seconds
        self._retention_days = retention_days
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._logger = logger if logger is not None else logging.getLogger(__name__)

    async def handle_tool_use(
        self,
        *,
        session_id: str,
        engineer_id: str,
        execution_id: str | None,
        tool_use_id: str,
        tool_input: str | Mapping[str, object],
    ) -> ToolResult:
        """Route one ``ask_devops_agent`` invocation to a tool result.

        Extracts the query, gates it through the fail-closed guardrail
        (Req 4.1, 4.4), and on PASS resolves the session's chat
        (Req 3.2, 3.9, 3.10) and returns the agent's accumulated answer
        (Req 3.3, 3.4). Every expected failure — refusal, agent error,
        timeout, or store error — is converted into an error tool
        result whose message Nova_Sonic verbalizes (Req 3.7, 3.8, 4.3),
        so no expected exception escapes to the stream consumer.

        Args:
            session_id: Identifier of the Voice_Session the ``toolUse``
                event belongs to; keys the chat mapping and scopes all
                log entries (Req 3.2, 4.5).
            engineer_id: Identity of the engineer driving the session,
                recorded in the audit entry when the guardrail blocks
                (Req 4.5).
            execution_id: DevOps_Agent execution the session was opened
                from, or ``None`` when the session is not
                incident-scoped; applied when a chat must be created
                (Req 3.5, 3.6).
            tool_use_id: The ``toolUseId`` of the ``toolUse`` event,
                echoed in the returned result (Req 3.4).
            tool_input: The tool input as delivered by the stream
                consumer: parsed mapping or raw input string.

        Returns:
            The ``ToolResult`` to return into the stream: the agent's
            answer as ``{"answer": ...}`` on success, or an
            ``{"error": ...}`` refusal or failure indication.
        """
        query = _extract_query(tool_input)
        if not query.strip():
            return _error_result(tool_use_id, EMPTY_QUERY_MESSAGE)
        decision = self._mutation_backstop(query) or await self._gate(query)
        if not decision.is_pass:
            self._audit_block(
                query=query,
                session_id=session_id,
                engineer_id=engineer_id,
                reason=decision.reason,
            )
            return _error_result(tool_use_id, REFUSAL_MESSAGE)
        try:
            chat_id = await self._resolve_chat_id(
                session_id=session_id, execution_id=execution_id
            )
            answer = await self._ask_agent(chat_id, query)
        except AgentTimeoutError as error:
            self._log_agent_failure(error, session_id)
            return _error_result(tool_use_id, AGENT_TIMEOUT_MESSAGE)
        except (AgentRequestError, SessionStoreError) as error:
            self._log_agent_failure(error, session_id)
            if isinstance(error, AgentRequestError) and error.is_throttled:
                # "Busy" needs the opposite reaction to "broken": a generic
                # failure invites Nova_Sonic to retry at once, which is how
                # one question became five SendMessage calls in six seconds
                # and exhausted the agent's quota.
                return _error_result(tool_use_id, AGENT_BUSY_MESSAGE)
            return _error_result(tool_use_id, AGENT_FAILURE_MESSAGE)
        else:
            if not answer.strip():
                # Never hand Nova_Sonic an empty answer silently: log it
                # with the session id so an extraction or agent-side gap
                # is visible in the operational record.
                self._logger.warning(
                    "DevOps Agent returned an empty answer",
                    extra={
                        "event": _EMPTY_ANSWER_EVENT,
                        "session_id": session_id,
                    },
                )
                return _error_result(tool_use_id, EMPTY_ANSWER_MESSAGE)
            return _answer_result(tool_use_id, answer)

    def _mutation_backstop(self, query: str) -> Decision | None:
        """Block unambiguous mutation instructions without a guardrail call.

        Deterministic second line of defence behind the semantic guardrail
        topic, whose verdicts are probabilistic: imperative mutations such
        as "Create an access key for my IAM user" are answered
        ``action=NONE`` by the deployed topic even though they change AWS
        state (rationale and measurements in
        :mod:`app.domain.mutation_guard`). Only ever adds blocks — a
        ``None`` return means the request still faces the full fail-closed
        guardrail gate, so this can never turn a BLOCK into a pass.

        Args:
            query: The extracted engineer request text.

        Returns:
            A BLOCK :class:`Decision` with reason
            :data:`~app.domain.mutation_guard.MUTATION_INTENT_REASON` when
            the request unambiguously instructs a state change; ``None``
            when the guardrail should decide.
        """
        if has_mutation_intent(query):
            return Decision(
                outcome=DecisionOutcome.BLOCK, reason=MUTATION_INTENT_REASON
            )
        return None

    async def _gate(self, query: str) -> Decision:
        """Evaluate one query through the fail-closed guardrail gate.

        Args:
            query: The extracted engineer request text, evaluated in
                full before any part of it may reach the DevOps_Agent
                (Req 4.1, 4.2).

        Returns:
            The gate ``Decision``: the guardrail evaluation — or the
            ``GuardrailUnavailableError`` raised in its place
            (Req 4.6) — interpreted by the pure fail-closed
            ``domain.guardrail_policy.decide`` (Req 4.4, 4.7).
        """
        try:
            evaluation = await self._guardrail.evaluate(query)
        except GuardrailUnavailableError as error:
            return decide(error)
        return decide(evaluation)

    async def _resolve_chat_id(
        self, *, session_id: str, execution_id: str | None
    ) -> str:
        """Return the session's chat identifier, creating it when absent.

        Reads the persisted mapping first so every subsequent request
        of the session reuses the same chat (Req 3.9). When no mapping
        exists — first invocation, or a lost mapping (Req 3.2, 3.10) —
        a chat is created with the session's execution scoping
        (Req 3.5, 3.6) and persisted with a TTL computed from the
        injected clock (Req 8.4).

        Args:
            session_id: Identifier of the owning Voice_Session, keying
                the mapping.
            execution_id: Execution scope for a newly created chat, or
                ``None`` for an unscoped chat.

        Returns:
            The chat identifier to send the request on.

        Raises:
            SessionStoreError: If reading or persisting the mapping
                fails (Req 8.5).
            AgentRequestError: If ``CreateChat`` fails (Req 3.7).
        """
        chat_id = await self._store.get_chat_id(session_id)
        if chat_id is not None:
            return chat_id
        chat_id = await self._agent.create_chat(execution_id)
        ttl = compute_ttl_from_epoch(int(self._clock()), self._retention_days)
        await self._store.put_chat_id(
            session_id, chat_id, ttl=ttl, execution_id=execution_id
        )
        return chat_id

    async def _ask_agent(self, chat_id: str, query: str) -> str:
        """Send one query and accumulate the streamed answer in order.

        Consumption of the streamed response is bounded by the
        configured budget via ``asyncio.timeout``: on expiry the stream
        is no longer consumed and the timeout surfaces as
        ``AgentTimeoutError`` (Req 3.8). Chunks are accumulated in
        arrival order so the result equals their exact concatenation
        (Req 3.3, design Property 4).

        Args:
            chat_id: Chat to send the request on — the session's
                persisted chat (Req 3.9).
            query: The engineer request text, forwarded verbatim.

        Returns:
            The complete accumulated response text (Req 3.3).

        Raises:
            AgentTimeoutError: If the streamed response does not
                complete within the configured budget (Req 3.8).
            AgentRequestError: If the ``SendMessage`` call fails, on
                call or mid-stream (Req 3.7).
        """
        accumulator = ChunkAccumulator()
        try:
            async with asyncio.timeout(self._agent_timeout_seconds):
                async for chunk in self._agent.send_message(chat_id, query):
                    accumulator.append(chunk)
        except TimeoutError as error:
            raise AgentTimeoutError(self._agent_timeout_seconds) from error
        return accumulator.result()

    def _audit_block(
        self, *, query: str, session_id: str, engineer_id: str, reason: str
    ) -> None:
        """Write the audit log entry for one guardrail-blocked request.

        Emits a WARNING entry carrying the blocked request content, the
        Voice_Session identifier, the engineer identity, and a
        timestamp from the injected clock (Req 4.5), plus the stable
        decision reason from ``domain.guardrail_policy``.

        Args:
            query: The blocked request content, recorded verbatim.
            session_id: Identifier of the Voice_Session the request
                belonged to.
            engineer_id: Identity of the engineer whose request was
                blocked.
            reason: Stable audit reason token of the BLOCK decision,
                for example ``intervened`` or ``unavailable:timeout``.
        """
        self._logger.warning(
            "Guardrail blocked ask_devops_agent request",
            extra={
                "event": _AUDIT_BLOCKED_EVENT,
                "blocked_content": query,
                "session_id": session_id,
                "engineer_id": engineer_id,
                "reason": reason,
                "timestamp": _iso_utc(self._clock()),
            },
        )

    def _log_agent_failure(
        self, error: DevOpsAgentError | SessionStoreError, session_id: str
    ) -> None:
        """Log one failed agent tool call with its exception class.

        Emits an ERROR entry naming the exception class and the
        Voice_Session identifier (Req 3.7), with the exception chain
        attached; the query and any partial answer are never included.

        Args:
            error: The failure being reported: an agent request error,
                a stream-budget timeout, or a session store error.
            session_id: Identifier of the Voice_Session the tool call
                belonged to.
        """
        self._logger.error(
            "ask_devops_agent tool call failed",
            exc_info=error,
            extra={
                "event": _AGENT_FAILED_EVENT,
                "exception_class": type(error).__name__,
                "session_id": session_id,
            },
        )
