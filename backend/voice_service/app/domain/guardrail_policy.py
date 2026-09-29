"""Guardrail fail-closed decision function (Req 4.4, 4.6, 4.7).

Pure decision core of the guardrail gate. The tool router forwards an
engineer request to the DevOps_Agent only when :func:`decide` returns an
explicit PASS decision and refuses the request on every other outcome
(design Property 7). The decision table is the design's Guardrail
Evaluation Model:

- ``action == "NONE"`` with every Automated Reasoning finding VALID →
  PASS (reason ``pass``).
- ``action == "GUARDRAIL_INTERVENED"`` → BLOCK (reason ``intervened``).
- Any Automated Reasoning finding of INVALID / SATISFIABLE / IMPOSSIBLE /
  TRANSLATION_AMBIGUOUS / NO_TRANSLATION → BLOCK (reason
  ``finding:<TYPE>``).
- SDK exception, timeout, or throttle — normalized by the guardrail
  adapter into ``GuardrailUnavailableError`` — → BLOCK (reason
  ``unavailable:<reason>``, Req 4.6).
- Anything else — unknown action, missing fields, or an uninterpretable
  finding — → BLOCK (reason ``malformed``, Req 4.7).

An evaluation with ``action == "NONE"`` and zero findings is a PASS: the
design's pass condition is "action NONE and all findings VALID", which
zero findings satisfies vacuously. ``action`` is the guardrail's own
authoritative verdict — a clean ``ApplyGuardrail`` response for a
compliant request may carry no Automated Reasoning findings at all — and
the findings scan is defense-in-depth that can only turn passes into
blocks, never blocks into passes.

This is a pure domain module: no I/O, no SDK imports, and deterministic
for identical inputs (Req 17.6). The guardrail adapter maps raw
``ApplyGuardrail`` responses into :class:`GuardrailEvaluation` via the
lenient :meth:`GuardrailEvaluation.from_response` parser — unrecognized
structure never raises; it degrades to malformed markers (``action=None``
or ``Finding(result=None)``) that :func:`decide` maps to a ``malformed``
BLOCK — and surfaces SDK failures as ``GuardrailUnavailableError``.

On BLOCK the tool router returns :data:`REFUSAL_MESSAGE` as the tool
result so Nova_Sonic verbally informs the engineer that only read and
diagnostic operations are supported (Req 4.3), and writes an audit log
entry carrying ``Decision.reason`` alongside the blocked content, session
id, engineer identity, and timestamp (Req 4.5). The reason vocabulary is
stable for those audit logs: ``pass``, ``intervened``, ``finding:<TYPE>``,
``unavailable:<reason>``, and ``malformed``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final, TypeIs

from app.exceptions import GuardrailUnavailableError

__all__ = [
    "ACTION_GUARDRAIL_INTERVENED",
    "ACTION_NONE",
    "BLOCKING_FINDINGS",
    "FINDING_VALID",
    "REFUSAL_MESSAGE",
    "Decision",
    "DecisionOutcome",
    "Finding",
    "GuardrailEvaluation",
    "decide",
]

ACTION_NONE: Final = "NONE"
"""``ApplyGuardrail`` action value meaning the guardrail did not intervene."""

ACTION_GUARDRAIL_INTERVENED: Final = "GUARDRAIL_INTERVENED"
"""``ApplyGuardrail`` action value meaning the guardrail blocked the input."""

FINDING_VALID: Final = "VALID"
"""The only Automated Reasoning finding type compatible with a PASS."""

BLOCKING_FINDINGS: Final[frozenset[str]] = frozenset(
    {
        "INVALID",
        "SATISFIABLE",
        "IMPOSSIBLE",
        "TRANSLATION_AMBIGUOUS",
        "NO_TRANSLATION",
    }
)
"""Recognized non-VALID finding types, each blocking with ``finding:<TYPE>``."""

REFUSAL_MESSAGE: Final = (
    "Your request was blocked by the security guardrail. This portal "
    "supports only read and diagnostic operations."
)
"""Canonical refusal the tool router returns on BLOCK, spoken by Nova_Sonic (Req 4.3)."""

_FINDING_KEY_RESULTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "valid": "VALID",
        "invalid": "INVALID",
        "satisfiable": "SATISFIABLE",
        "impossible": "IMPOSSIBLE",
        "translationAmbiguous": "TRANSLATION_AMBIGUOUS",
        "noTranslation": "NO_TRANSLATION",
    }
)
"""Tagged-union key of a raw Automated Reasoning finding → normalized type."""

_REASON_PASS: Final = "pass"
"""Audit reason for the single explicit PASS row of the decision table."""

_REASON_INTERVENED: Final = "intervened"
"""Audit reason for a ``GUARDRAIL_INTERVENED`` action."""

_REASON_MALFORMED: Final = "malformed"
"""Audit reason for anything not recognized by the decision table (Req 4.7)."""


@unique
class DecisionOutcome(StrEnum):
    """Binary outcome of the guardrail gate decision.

    The values are the uppercase ``PASS`` / ``BLOCK`` tokens from the
    design's Guardrail Evaluation Model, suitable for audit log entries.
    """

    PASS = "PASS"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class Decision:
    """Immutable guardrail gate decision (Req 4.4).

    Attributes:
        outcome: Whether the request may be forwarded to the DevOps_Agent
            (``PASS``) or must be refused (``BLOCK``).
        reason: Stable audit-log token explaining the outcome: ``pass``,
            ``intervened``, ``finding:<TYPE>``, ``unavailable:<reason>``,
            or ``malformed`` (Req 4.5).
    """

    outcome: DecisionOutcome
    reason: str

    @property
    def is_pass(self) -> bool:
        """Return whether the request may be forwarded to the DevOps_Agent.

        Returns:
            ``True`` only for an explicit PASS outcome; ``False`` for
            every BLOCK, so gate call sites read as a fail-closed binary
            check (Req 4.4).
        """
        return self.outcome is DecisionOutcome.PASS


@dataclass(frozen=True, slots=True)
class Finding:
    """One Automated Reasoning finding extracted from an assessment.

    Attributes:
        result: Normalized uppercase finding type — ``VALID`` or one of
            ``BLOCKING_FINDINGS`` — or ``None`` when the raw finding could
            not be interpreted (a malformed marker). Matching in
            :func:`decide` is exact and case-sensitive; any unrecognized
            value blocks as ``malformed`` (Req 4.7).
    """

    result: str | None


_MALFORMED_FINDING: Final = Finding(result=None)
"""Marker for uninterpretable finding structure; blocks as ``malformed``."""


@dataclass(frozen=True, slots=True)
class GuardrailEvaluation:
    """Parsed ``ApplyGuardrail`` response as consumed by :func:`decide`.

    Attributes:
        action: The response's top-level ``action`` value — ``"NONE"`` or
            ``"GUARDRAIL_INTERVENED"`` on a well-formed response — or
            ``None`` when the field was missing or not a string.
        findings: Automated Reasoning findings extracted from all
            assessments, in response order; empty when the response
            carried none.
    """

    action: str | None
    findings: tuple[Finding, ...] = ()

    @classmethod
    def from_response(cls, response: Mapping[str, object]) -> GuardrailEvaluation:
        """Parse a raw ``ApplyGuardrail`` response body leniently.

        Reads the top-level ``action`` and every
        ``assessments[].automatedReasoningPolicy.findings[]`` entry, where
        each raw finding is a tagged union carrying exactly one key naming
        its type (``valid``, ``invalid``, ``satisfiable``, ``impossible``,
        ``translationAmbiguous``, or ``noTranslation``). The parser never
        raises: unknown or missing structure degrades to malformed markers
        (``action=None`` / ``Finding(result=None)``) that :func:`decide`
        maps to a ``malformed`` BLOCK, keeping the gate fail-closed
        (Req 4.7). Assessments without an ``automatedReasoningPolicy`` key
        (for example content- or topic-policy assessments) contribute no
        findings.

        Args:
            response: Deserialized ``ApplyGuardrail`` response body as a
                mapping, in the shape returned by the bedrock-runtime API.

        Returns:
            A ``GuardrailEvaluation`` carrying the extracted action and
            findings, with malformed markers in place of anything
            uninterpretable.
        """
        action_value = response.get("action")
        action = action_value if isinstance(action_value, str) else None
        return cls(action=action, findings=_parse_findings(response))


def _is_array(value: object) -> TypeIs[Sequence[object]]:
    """Return whether ``value`` is a JSON-style array (non-string sequence).

    Args:
        value: Candidate value taken from a deserialized response body.

    Returns:
        ``True`` when ``value`` is a sequence other than ``str``,
        ``bytes``, or ``bytearray``, so iterating it yields elements
        rather than characters.
    """
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _parse_finding(raw_finding: object) -> Finding:
    """Parse one raw tagged-union Automated Reasoning finding.

    Args:
        raw_finding: One entry of a raw ``findings`` array; on a
            well-formed response, a mapping with exactly one key naming
            the finding type.

    Returns:
        A ``Finding`` with the normalized type, or the malformed marker
        when the entry is not a single-key mapping with a recognized
        tagged-union key.
    """
    if not isinstance(raw_finding, Mapping) or len(raw_finding) != 1:
        return _MALFORMED_FINDING
    key = next(iter(raw_finding))
    if not isinstance(key, str):
        return _MALFORMED_FINDING
    result = _FINDING_KEY_RESULTS.get(key)
    return Finding(result=result) if result is not None else _MALFORMED_FINDING


def _parse_assessment(assessment: object) -> list[Finding]:
    """Extract the Automated Reasoning findings of one raw assessment.

    Args:
        assessment: One entry of the raw ``assessments`` array.

    Returns:
        The parsed findings of the assessment's
        ``automatedReasoningPolicy`` block: empty when the assessment is a
        mapping without that key (an assessment for another policy type),
        one malformed marker when the block or the assessment itself is
        not shaped as expected, and one ``Finding`` per raw entry
        otherwise.
    """
    if not isinstance(assessment, Mapping):
        return [_MALFORMED_FINDING]
    if "automatedReasoningPolicy" not in assessment:
        return []
    policy = assessment["automatedReasoningPolicy"]
    if not isinstance(policy, Mapping):
        return [_MALFORMED_FINDING]
    raw_findings = policy.get("findings")
    if not _is_array(raw_findings):
        return [_MALFORMED_FINDING]
    return [_parse_finding(raw_finding) for raw_finding in raw_findings]


def _parse_findings(response: Mapping[str, object]) -> tuple[Finding, ...]:
    """Extract all Automated Reasoning findings from a raw response body.

    Args:
        response: Deserialized ``ApplyGuardrail`` response body.

    Returns:
        The findings of every assessment in response order. A missing
        ``assessments`` key yields no findings (the clean-pass response
        shape); an ``assessments`` value that is not an array yields one
        malformed marker.
    """
    if "assessments" not in response:
        return ()
    assessments = response["assessments"]
    if not _is_array(assessments):
        return (_MALFORMED_FINDING,)
    findings: list[Finding] = []
    for assessment in assessments:
        findings.extend(_parse_assessment(assessment))
    return tuple(findings)


def _decide_evaluation(evaluation: GuardrailEvaluation) -> Decision:
    """Apply the decision table to a parsed guardrail evaluation.

    Args:
        evaluation: Parsed ``ApplyGuardrail`` response.

    Returns:
        PASS only for ``action == "NONE"`` with every finding VALID
        (vacuously true for zero findings); BLOCK with reason
        ``intervened`` for ``GUARDRAIL_INTERVENED``, ``finding:<TYPE>``
        for the first recognized non-VALID finding, or ``malformed`` for
        any other action value or uninterpretable finding (Req 4.4, 4.7).
    """
    if evaluation.action == ACTION_GUARDRAIL_INTERVENED:
        return Decision(outcome=DecisionOutcome.BLOCK, reason=_REASON_INTERVENED)
    if evaluation.action != ACTION_NONE:
        return Decision(outcome=DecisionOutcome.BLOCK, reason=_REASON_MALFORMED)
    for finding in evaluation.findings:
        if finding.result == FINDING_VALID:
            continue
        if finding.result in BLOCKING_FINDINGS:
            return Decision(
                outcome=DecisionOutcome.BLOCK,
                reason=f"finding:{finding.result}",
            )
        return Decision(outcome=DecisionOutcome.BLOCK, reason=_REASON_MALFORMED)
    return Decision(outcome=DecisionOutcome.PASS, reason=_REASON_PASS)


def decide(
    evaluation_or_error: GuardrailEvaluation | GuardrailUnavailableError,
) -> Decision:
    """Decide whether a guardrail-evaluated request may be forwarded.

    Implements the design's fail-closed decision table: the only path to
    PASS is a well-formed evaluation with ``action == "NONE"`` whose
    findings are all VALID (vacuously true for zero findings). Every
    other input — an intervention, any non-VALID finding, an unavailable
    guardrail, or anything unrecognized — blocks, so the DevOps_Agent
    only ever receives requests the guardrail explicitly cleared
    (Req 4.4, 4.6, 4.7).

    Args:
        evaluation_or_error: Either the parsed guardrail evaluation or
            the ``GuardrailUnavailableError`` the guardrail adapter
            raised in its place (SDK exception, timeout, or throttle,
            Req 4.6).

    Returns:
        The gate ``Decision``: outcome PASS with reason ``pass``, or
        outcome BLOCK with an audit reason of ``intervened``,
        ``finding:<TYPE>``, ``unavailable:<reason>``, or ``malformed``.
    """
    if isinstance(evaluation_or_error, GuardrailUnavailableError):
        return Decision(
            outcome=DecisionOutcome.BLOCK,
            reason=f"unavailable:{evaluation_or_error.reason}",
        )
    return _decide_evaluation(evaluation_or_error)
