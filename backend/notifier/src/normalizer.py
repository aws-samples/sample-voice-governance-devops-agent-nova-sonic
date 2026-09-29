"""Pure event normalizer for the Notifier Lambda (Req 5.5, 5.6).

Maps the three EventBridge incident event shapes — CloudWatch Alarm state
changes, Incident Manager incidents, and DevOps Agent findings — onto the
design's Incident_Notification payload, the single schema published to
AppSync Events, Web Push, and SNS.

Normalization is **total**: :func:`normalize` never raises, regardless of
missing, wrong-typed, or otherwise malformed fields; every degraded field
falls back to a documented default (Property 8: notification normalization
is total and preserving). It is also **pure**: the notification identity and
the current time are injected by the caller, and the module performs no I/O
(an import-linter contract keeps it free of AWS SDK imports).

Source classification (by the top-level ``source`` field):

* ``"aws.cloudwatch"`` maps to ``cloudwatch-alarm``.
* ``"aws.ssm-incidents"`` maps to ``incident-manager``.
* Anything else — including ``"aws.aidevops"`` and a missing or non-string
  ``source`` — maps to ``devops-agent-finding``. The catch-all keeps
  normalization total: the Lambda is subscribed to exactly three EventBridge
  rule patterns, so an unrecognized shape degrades to the finding mapping
  instead of failing.

Per-source field mappings and severity heuristics:

* CloudWatch Alarm: ``summary`` joins ``detail.alarmName`` and
  ``detail.state.reason`` as ``"<name>: <reason>"``, using whichever part is
  present. ``severity`` scans ``detail.configuration.description`` for a
  whole-word, case-insensitive severity token, most severe wins
  (``critical`` over ``high`` over ``medium`` over ``low``); with no token,
  ``detail.state.value`` equal to ``"ALARM"`` (case-insensitive) yields
  ``high`` and any other state yields ``medium``.
* Incident Manager: ``summary`` comes from ``detail.title``; ``severity``
  maps the integer ``detail.impact``: 1 to ``critical``, 2 to ``high``, 3 to
  ``medium``, 4 or more to ``low``, and any other value (non-integer,
  boolean, or below 1) to ``medium``.
* DevOps Agent finding: ``summary`` comes from ``detail.summary`` with
  ``detail.title`` as fallback; ``severity`` is ``detail.severity`` stripped
  and lowercased when that yields an allowed value, else ``medium``.

Rules shared by all sources:

* ``summary`` falls back to ``"Incident from <source label>"`` when the
  mapped fields are missing or blank, so it is never empty.
* ``timestamp`` echoes the event's ``time`` when it parses as ISO-8601,
  normalized to UTC (a value without an offset is assumed to already be
  UTC); otherwise the injected ``now_iso`` is used verbatim.
* ``executionId`` is extracted leniently — ``detail.executionId`` first,
  then ``detail.execution.id`` — for every source shape, and appears in the
  payload if and only if the event provided a non-empty string (Req 5.6).
* ``detail`` passes the event's ``detail`` mapping through unchanged when it
  is a mapping and is omitted otherwise.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

__all__ = ["IncidentNotification", "Severity", "Source", "normalize"]

type Source = Literal["cloudwatch-alarm", "incident-manager", "devops-agent-finding"]
"""Portal source label distinguishing the three EventBridge event shapes."""

type Severity = Literal["critical", "high", "medium", "low"]
"""Allowed Incident_Notification severity values (Req 5.5)."""

_SEVERITIES: tuple[Severity, ...] = ("critical", "high", "medium", "low")
"""Severity scan order, most severe first, so the strongest match wins."""

_SOURCE_LABELS: Mapping[Source, str] = {
    "cloudwatch-alarm": "CloudWatch alarm",
    "incident-manager": "Incident Manager",
    "devops-agent-finding": "DevOps Agent",
}
"""Human-readable source names used in fallback summaries."""


@dataclass(frozen=True, slots=True)
class IncidentNotification:
    """One normalized incident notification (Req 5.5, 5.6).

    Immutable result of :func:`normalize`. The identical payload, rendered
    by :meth:`to_payload`, is published to AppSync Events, Web Push, and
    SNS.

    Attributes:
        notification_id: Caller-supplied unique identifier (wire key
            ``notificationId``).
        source: Portal source label for the originating event shape.
        summary: Non-empty human-readable incident summary.
        severity: One of ``critical``, ``high``, ``medium``, or ``low``.
        timestamp: ISO-8601 UTC time of the incident event.
        execution_id: DevOps Agent execution identifier when the source
            event provided one, else ``None`` (wire key ``executionId``).
        detail: The source event's ``detail`` mapping passed through
            unchanged, or ``None`` when the event carried no such mapping.
        summary_is_fallback: ``True`` when the event carried no usable
            summary field and :attr:`summary` is the generic ``"Incident
            from <source label>"`` placeholder — the event announced no
            incident content of its own. Not part of the wire payload; the
            handler uses it to decide whether an event is worth notifying
            about at all.
    """

    notification_id: str
    source: Source
    summary: str
    severity: Severity
    timestamp: str
    execution_id: str | None
    detail: Mapping[str, object] | None
    summary_is_fallback: bool = False

    def to_payload(self) -> dict[str, object]:
        """Render the notification as the design's camelCase wire payload.

        Returns:
            Dict carrying the required keys ``notificationId``, ``source``,
            ``summary``, ``severity``, and ``timestamp``, plus
            ``executionId`` if and only if ``execution_id`` is not ``None``
            (Req 5.6) and ``detail`` (a shallow ``dict`` copy) if and only
            if ``detail`` is not ``None``.
        """
        payload: dict[str, object] = {
            "notificationId": self.notification_id,
            "source": self.source,
            "summary": self.summary,
            "severity": self.severity,
            "timestamp": self.timestamp,
        }
        if self.execution_id is not None:
            payload["executionId"] = self.execution_id
        if self.detail is not None:
            payload["detail"] = dict(self.detail)
        return payload


def normalize(
    event: Mapping[str, object],
    *,
    notification_id: str,
    now_iso: str,
) -> IncidentNotification:
    """Map one EventBridge incident event to an Incident_Notification.

    Applies the module's per-source field mappings and severity heuristics
    (Req 5.5, 5.6). Total and pure: it never raises, performs no I/O, and
    the same arguments always produce an equal notification; malformed or
    missing fields degrade to the documented defaults.

    Args:
        event: The EventBridge event as a JSON-like mapping. Recognized
            top-level fields are ``source``, ``time``, and ``detail``.
        notification_id: Caller-generated unique identifier for the
            notification (identity is injected to keep this function pure).
        now_iso: Caller-supplied current time as an ISO-8601 UTC string,
            used as the timestamp fallback when the event's ``time`` is
            missing or unparseable (the clock is injected likewise).

    Returns:
        The normalized IncidentNotification with a non-empty summary, a
        severity from the allowed set, an ISO-8601 UTC timestamp, and an
        execution identifier if and only if the event provided one.
    """
    source = _classify(event.get("source"))
    detail = _mapping(event.get("detail"))
    if source == "cloudwatch-alarm":
        summary = _cloudwatch_summary(detail)
        severity = _cloudwatch_severity(detail)
    elif source == "incident-manager":
        summary = _incident_manager_summary(detail)
        severity = _incident_manager_severity(detail)
    else:
        summary = _devops_summary(detail)
        severity = _devops_severity(detail)
    summary_is_fallback = summary is None
    if summary is None:
        summary = f"Incident from {_SOURCE_LABELS[source]}"
    return IncidentNotification(
        notification_id=notification_id,
        source=source,
        summary=summary,
        severity=severity,
        timestamp=_timestamp(event.get("time"), now_iso),
        execution_id=_execution_id(detail),
        detail=detail,
        summary_is_fallback=summary_is_fallback,
    )


def _classify(source: object) -> Source:
    """Classify the event's ``source`` field into a portal source label.

    Args:
        source: The raw top-level ``source`` value from the event.

    Returns:
        ``cloudwatch-alarm`` for ``"aws.cloudwatch"``, ``incident-manager``
        for ``"aws.ssm-incidents"``, and ``devops-agent-finding`` for
        everything else (the documented catch-all).
    """
    if source == "aws.cloudwatch":
        return "cloudwatch-alarm"
    if source == "aws.ssm-incidents":
        return "incident-manager"
    return "devops-agent-finding"


def _cloudwatch_summary(detail: Mapping[str, object] | None) -> str | None:
    """Build the CloudWatch Alarm summary from alarm name and state reason.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        ``"<alarmName>: <state.reason>"`` when both parts are present,
        whichever single part is present otherwise, or ``None`` when
        neither is usable text.
    """
    if detail is None:
        return None
    name = _text(detail.get("alarmName"))
    state = _mapping(detail.get("state"))
    reason = _text(state.get("reason")) if state is not None else None
    if name is not None and reason is not None:
        return f"{name}: {reason}"
    return name or reason


def _cloudwatch_severity(detail: Mapping[str, object] | None) -> Severity:
    """Derive a CloudWatch Alarm severity via the documented heuristic.

    The heuristic scans ``configuration.description`` for a whole-word,
    case-insensitive severity token (most severe wins); with no token it
    falls back on the state value: ``ALARM`` (case-insensitive) means
    ``high``, anything else means ``medium``.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        The severity chosen by the heuristic; ``medium`` when nothing more
        specific applies.
    """
    if detail is None:
        return "medium"
    configuration = _mapping(detail.get("configuration"))
    if configuration is not None:
        description = _text(configuration.get("description"))
        if description is not None:
            token = _severity_token(description)
            if token is not None:
                return token
    state = _mapping(detail.get("state"))
    value = _text(state.get("value")) if state is not None else None
    if value is not None and value.upper() == "ALARM":
        return "high"
    return "medium"


def _incident_manager_summary(detail: Mapping[str, object] | None) -> str | None:
    """Extract the Incident Manager summary from the incident title.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        The non-blank ``title`` text, or ``None`` when absent.
    """
    if detail is None:
        return None
    return _text(detail.get("title"))


def _incident_manager_severity(detail: Mapping[str, object] | None) -> Severity:
    """Map the Incident Manager integer impact onto a severity.

    Mapping: impact 1 is ``critical``, 2 is ``high``, 3 is ``medium``, and
    4 or more is ``low``. Any other value — non-integer, boolean, or below
    1 — degrades to ``medium``.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        The mapped severity, or the ``medium`` fallback.
    """
    impact = detail.get("impact") if detail is not None else None
    if not isinstance(impact, int) or isinstance(impact, bool):
        return "medium"
    if impact == 1:
        return "critical"
    if impact == 2:
        return "high"
    if impact >= 4:
        return "low"
    return "medium"


def _devops_summary(detail: Mapping[str, object] | None) -> str | None:
    """Extract the DevOps Agent finding summary.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        The non-blank ``summary`` text, falling back to ``title``, or
        ``None`` when neither is usable text.
    """
    if detail is None:
        return None
    return _text(detail.get("summary")) or _text(detail.get("title"))


def _devops_severity(detail: Mapping[str, object] | None) -> Severity:
    """Normalize the DevOps Agent finding severity.

    ``detail.severity`` is stripped and lowercased; the result is used when
    it lands in the allowed set, else the severity degrades to ``medium``.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        The normalized severity, or the ``medium`` fallback.
    """
    raw = _text(detail.get("severity")) if detail is not None else None
    if raw is not None:
        recognized = _as_severity(raw)
        if recognized is not None:
            return recognized
    return "medium"


def _execution_id(detail: Mapping[str, object] | None) -> str | None:
    """Extract a DevOps Agent execution identifier leniently (Req 5.6).

    Checks ``detail.executionId`` first, then ``detail.execution.id``. Only
    a non-empty string counts as provided; any other value is treated as
    absent. The same extraction applies to all three source shapes, so the
    payload carries an ``executionId`` exactly when the event provided one.

    Args:
        detail: The event's ``detail`` mapping, or ``None``.

    Returns:
        The execution identifier verbatim, or ``None`` when the event did
        not provide one.
    """
    if detail is None:
        return None
    direct = detail.get("executionId")
    if isinstance(direct, str) and direct:
        return direct
    execution = _mapping(detail.get("execution"))
    if execution is not None:
        nested = execution.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _timestamp(raw: object, now_iso: str) -> str:
    """Choose the notification timestamp (Req 5.5).

    Args:
        raw: The event's top-level ``time`` value.
        now_iso: Injected current time as an ISO-8601 UTC string, used as
            the fallback.

    Returns:
        The event time normalized to ISO-8601 UTC when ``raw`` is a
        parseable ISO-8601 string (a naive value is assumed to already be
        UTC; an aware value is converted), else ``now_iso`` verbatim.
    """
    if not isinstance(raw, str):
        return now_iso
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return now_iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        utc = parsed.astimezone(UTC)
    except OverflowError:
        # Extreme dates near datetime.min/max overflow on offset conversion.
        return now_iso
    return utc.isoformat()


def _severity_token(text: str) -> Severity | None:
    """Scan free text for a whole-word severity token.

    Args:
        text: Free text to scan, typically an alarm description.

    Returns:
        The most severe token found (scanned in ``critical``, ``high``,
        ``medium``, ``low`` order, case-insensitively, on word boundaries),
        or ``None`` when no token occurs.
    """
    for name in _SEVERITIES:
        if re.search(rf"\b{name}\b", text, re.IGNORECASE):
            return name
    return None


def _as_severity(text: str) -> Severity | None:
    """Interpret text as an allowed severity value.

    Args:
        text: Candidate severity text.

    Returns:
        The matching severity when the stripped, lowercased text equals an
        allowed value, else ``None``.
    """
    lowered = text.strip().lower()
    for name in _SEVERITIES:
        if lowered == name:
            return name
    return None


def _text(value: object) -> str | None:
    """Return a value as usable text.

    Args:
        value: Arbitrary field content from the event.

    Returns:
        The stripped string when ``value`` is a string with non-whitespace
        content, else ``None``.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _mapping(value: object) -> Mapping[str, object] | None:
    """Return a value as a mapping.

    Args:
        value: Arbitrary field content from the event.

    Returns:
        ``value`` unchanged when it is a mapping, else ``None``.
    """
    if isinstance(value, Mapping):
        return value
    return None
