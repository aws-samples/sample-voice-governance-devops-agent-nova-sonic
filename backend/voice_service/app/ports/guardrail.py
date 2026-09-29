"""Abstract interface to the Bedrock guardrail evaluation (Req 4.1).

``GuardrailPort`` is the port boundary (Req 17.6) between the tool
router's guardrail gate and Amazon Bedrock Guardrails. The implementing
adapter (``adapters.guardrail_client``) calls the bedrock-runtime
``ApplyGuardrail`` API with the configured guardrail id and version and
``source=INPUT``, against the guardrail carrying the Automated Reasoning
read-only-operations policy (Req 4.1, 4.2); test suites substitute the
deterministic in-memory ``FakeGuardrail``.

The port returns the *parsed* evaluation, not a decision: the gate feeds
the ``GuardrailEvaluation`` — or the ``GuardrailUnavailableError`` the
implementation raised in its place — into the pure fail-closed decision
function ``domain.guardrail_policy.decide`` (Req 4.4, 4.6, 4.7). That
split keeps every blocking rule in the fully property-testable domain
module, while the port stays a thin evaluation boundary.

Implementations raise ``GuardrailUnavailableError`` whenever an
evaluation cannot be completed — SDK exception, timeout, or throttle
(Req 4.6). The error is not a gate bypass: ``decide`` maps it to a BLOCK
with reason ``unavailable:<reason>``, so an unavailable guardrail always
refuses the request. This module imports no SDK (Req 17.6).
"""

from abc import ABC, abstractmethod

from app.domain.guardrail_policy import GuardrailEvaluation

__all__ = ["GuardrailPort"]


class GuardrailPort(ABC):
    """The guardrail evaluation call, as the guardrail gate sees it.

    Abstract base class over one operation: evaluating engineer request
    text against the read-only-operations Guardrail before anything is
    forwarded to the DevOps_Agent (Req 4.1, 4.4). Stateless; the
    fail-closed interpretation of results and failures belongs to
    ``domain.guardrail_policy.decide``.
    """

    @abstractmethod
    async def evaluate(self, text: str) -> GuardrailEvaluation:
        """Evaluate one engineer request against the Guardrail.

        Applies the configured guardrail (Automated Reasoning read-only
        policy, ``source=INPUT``) to the request text and returns the
        response parsed into the domain evaluation shape — via the
        lenient ``GuardrailEvaluation.from_response`` parser, so
        malformed responses degrade to markers that ``decide`` blocks as
        ``malformed`` (Req 4.7) rather than raising here.

        Args:
            text: The engineer request text extracted from the
                ``ask_devops_agent`` tool input, evaluated before any
                part of it may reach the DevOps_Agent (Req 4.1, 4.2).

        Returns:
            The parsed guardrail evaluation carrying the response's
            ``action`` and its Automated Reasoning findings, ready for
            ``domain.guardrail_policy.decide``.

        Raises:
            GuardrailUnavailableError: If the evaluation cannot be
                completed — SDK exception, timeout, or throttle
                (Req 4.6). The caller passes the error itself to
                ``decide``, which turns it into a BLOCK.
        """
