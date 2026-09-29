# Feature: nova-sonic-support-portal, Property 8: Notification normalization is total and preserving
"""Property test: notification normalization is total and preserving.

**Validates: Requirements 5.5, 5.6**

For any incident event from any of the three source shapes (CloudWatch
Alarm, Incident Manager, DevOps Agent finding) with arbitrary field
content, the normalizer produces an Incident_Notification containing a
non-empty summary, a severity from the allowed set, a valid ISO-8601
timestamp, and an executionId if and only if the source event provided
one (Property 8).

The suite drives :func:`src.normalizer.normalize` with events built from
recursively generated JSON-like values — scalars, lists, and mappings of
bounded depth — where ``source``, ``time``, and ``detail`` are each
present or absent, well-formed or arbitrarily malformed. Totality is
implicit in every run: a raise anywhere fails the test. On top of the
arbitrary-event checks, the payload contract of
:meth:`~src.normalizer.IncidentNotification.to_payload` (required keys,
``executionId`` presence if and only if provided, ``detail``
passthrough), determinism, and the timestamp echo/fallback rule are each
asserted, and one focused case per source shape pins the documented
severity heuristics: the CloudWatch description token scan with
most-severe-wins precedence, the Incident Manager integer impact
mapping, and the DevOps Agent case/whitespace severity normalization.

The executionId oracle re-derives the documented uniform extraction rule
(``detail.executionId`` first, then ``detail.execution.id``, non-empty
strings only) independently of the implementation (Req 5.6).
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

from hypothesis import event as record_coverage
from hypothesis import given, settings
from hypothesis import strategies as st

from src.normalizer import IncidentNotification, normalize

_NOTIFICATION_ID: Final = "7d5aa819-0d5c-44a1-9e14-c6c7f6c5b7e3"
"""Fixed caller-injected notification identity used for every example."""

_NOW_ISO: Final = "2024-01-01T00:00:00+00:00"
"""Fixed caller-injected clock value used as the timestamp fallback."""

_ALLOWED_SEVERITIES: Final = frozenset({"critical", "high", "medium", "low"})
"""The allowed Incident_Notification severity set (Req 5.5)."""

_SEVERITY_ORDER: Final = ("critical", "high", "medium", "low")
"""Severity tokens ordered most severe first, mirroring the design scan."""

_NOISE_WORDS: Final = ("alarm", "cpu", "latency", "disk", "requests", "threshold")
"""Description filler words that contain no severity token."""

_IMPACT_SEVERITIES: Final = {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "low"}
"""Documented Incident Manager impact-to-severity mapping."""

_json_scalars: Final = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False),
    st.text(max_size=12),
)
"""Arbitrary JSON scalar field content."""

_json_values: Final = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=6), children, max_size=3),
    ),
    max_leaves=8,
)
"""Arbitrary JSON-like values: scalars, lists, and mappings, bounded depth."""

_source_values: Final = st.one_of(
    st.sampled_from(("aws.cloudwatch", "aws.ssm-incidents", "aws.aidevops")),
    st.text(max_size=16),
    st.none(),
    st.integers(),
)
"""Top-level ``source`` values: the three real ones plus arbitrary junk."""

_time_values: Final = st.one_of(
    st.datetimes().map(lambda moment: moment.isoformat()),
    st.text(max_size=20),
    st.none(),
    st.integers(),
)
"""Top-level ``time`` values: valid ISO strings plus arbitrary junk."""

_id_candidates: Final = st.one_of(
    st.text(min_size=1, max_size=12),
    st.text(min_size=1, max_size=8).map(lambda suffix: f"exec-{suffix}"),
    st.just(""),
    st.none(),
    st.integers(),
    st.booleans(),
)
"""Candidate executionId values: non-empty strings count as provided,
empty strings and non-strings count as absent (Req 5.6)."""

_labels: Final = st.text(
    alphabet=st.characters(codec="ascii", categories=("L", "N")),
    min_size=1,
    max_size=12,
)
"""Strip-stable non-blank text for alarm names, titles, and summaries."""


@st.composite
def _seeded_details(draw: st.DrawFn) -> dict[str, object]:
    """Draw a ``detail`` mapping seeded with the recognized field names.

    Starts from an arbitrary mapping and independently overlays each field
    the normalizer reads — ``executionId``, ``execution``, the CloudWatch,
    Incident Manager, and DevOps Agent fields — with content that is
    sometimes well-typed and sometimes arbitrary junk.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        The seeded detail mapping.
    """
    detail: dict[str, object] = dict(
        draw(st.dictionaries(st.text(max_size=6), _json_values, max_size=2))
    )
    if draw(st.booleans()):
        detail["executionId"] = draw(_id_candidates)
    if draw(st.booleans()):
        if draw(st.booleans()):
            execution: dict[str, object] = {}
            if draw(st.booleans()):
                execution["id"] = draw(_id_candidates)
            detail["execution"] = execution
        else:
            detail["execution"] = draw(_json_values)
    if draw(st.booleans()):
        detail["alarmName"] = draw(_json_scalars)
    if draw(st.booleans()):
        detail["state"] = {
            "value": draw(_json_scalars),
            "reason": draw(_json_scalars),
        }
    if draw(st.booleans()):
        description = st.one_of(
            _json_scalars,
            st.sampled_from(("critical breach", "HIGH cpu", "all low")),
        )
        detail["configuration"] = {"description": draw(description)}
    if draw(st.booleans()):
        detail["title"] = draw(_json_scalars)
    if draw(st.booleans()):
        detail["summary"] = draw(_json_scalars)
    if draw(st.booleans()):
        severity = st.one_of(
            _json_scalars,
            st.sampled_from(("critical", " HIGH ", "Medium", "sev2")),
        )
        detail["severity"] = draw(severity)
    if draw(st.booleans()):
        detail["impact"] = draw(
            st.one_of(_json_scalars, st.integers(-1, 6), st.booleans())
        )
    return detail


@st.composite
def _events(draw: st.DrawFn) -> dict[str, object]:
    """Draw an arbitrary EventBridge-like incident event.

    Arbitrary extra keys are always present; ``source``, ``time``, and
    ``detail`` are each independently absent, well-formed, or malformed,
    covering all three source shapes plus the catch-all.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        The event mapping handed to the normalizer.
    """
    event: dict[str, object] = dict(
        draw(st.dictionaries(st.text(max_size=6), _json_values, max_size=2))
    )
    if draw(st.booleans()):
        event["source"] = draw(_source_values)
    if draw(st.booleans()):
        event["time"] = draw(_time_values)
    if draw(st.booleans()):
        event["detail"] = draw(st.one_of(st.none(), _json_values, _seeded_details()))
    return event


@st.composite
def _execution_focused_events(draw: st.DrawFn) -> dict[str, object]:
    """Draw events concentrating on the executionId extraction shapes.

    The detail mapping always exists and frequently carries the
    ``executionId`` and ``execution.id`` slots with candidate values on
    both sides of the provided/absent line, so the if-and-only-if check
    exercises positives and negatives densely.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        The event mapping handed to the normalizer.
    """
    detail: dict[str, object] = dict(
        draw(st.dictionaries(st.text(max_size=6), _json_values, max_size=2))
    )
    if draw(st.booleans()):
        detail["executionId"] = draw(_id_candidates)
    if draw(st.booleans()):
        execution: dict[str, object] = {}
        if draw(st.booleans()):
            execution["id"] = draw(_id_candidates)
        detail["execution"] = execution
    return {"source": draw(_source_values), "detail": detail}


def _provided_execution_id(event: Mapping[str, object]) -> str | None:
    """Re-derive the executionId the source event provided, if any.

    Independent restatement of the documented uniform extraction rule
    (Req 5.6): ``detail.executionId`` first, then ``detail.execution.id``,
    where only a non-empty string counts as provided, for every source
    shape alike.

    Args:
        event: The source event handed to the normalizer.

    Returns:
        The provided execution identifier, or ``None`` when the event did
        not provide one.
    """
    detail = event.get("detail")
    if not isinstance(detail, Mapping):
        return None
    direct = detail.get("executionId")
    if isinstance(direct, str) and direct:
        return direct
    execution = detail.get("execution")
    if isinstance(execution, Mapping):
        nested = execution.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _parses_as_isoformat(value: str) -> bool:
    """Check whether text parses with ``datetime.fromisoformat``.

    Args:
        value: Candidate timestamp text.

    Returns:
        ``True`` when parsing succeeds, ``False`` when it raises
        ``ValueError``.
    """
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


@given(event=_events())
@settings(max_examples=100, deadline=None)
def test_normalize_is_total_and_payload_well_formed(event: dict[str, object]) -> None:
    """Any event yields a well-formed notification and payload.

    Totality is implicit: the call must not raise for any generated
    event. The result carries a non-empty summary, a severity from the
    allowed set, and a timestamp ``datetime.fromisoformat`` accepts
    (Req 5.5); the source classifies by the documented rule with the
    catch-all; the payload echoes every required field and passes
    ``detail`` through if and only if the event carried a mapping.

    Args:
        event: Arbitrary EventBridge-like incident event.
    """
    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)

    assert isinstance(result, IncidentNotification)
    assert isinstance(result.summary, str)
    assert result.summary.strip()
    assert result.severity in _ALLOWED_SEVERITIES
    parsed_timestamp = datetime.fromisoformat(result.timestamp)
    assert isinstance(parsed_timestamp, datetime)

    raw_source = event.get("source")
    if raw_source == "aws.cloudwatch":
        assert result.source == "cloudwatch-alarm"
    elif raw_source == "aws.ssm-incidents":
        assert result.source == "incident-manager"
    else:
        assert result.source == "devops-agent-finding"

    payload = result.to_payload()
    assert payload["notificationId"] == _NOTIFICATION_ID
    assert payload["source"] == result.source
    assert payload["summary"] == result.summary
    assert payload["severity"] == result.severity
    assert payload["timestamp"] == result.timestamp

    raw_detail = event.get("detail")
    if isinstance(raw_detail, Mapping):
        assert result.detail == raw_detail
        assert payload["detail"] == dict(raw_detail)
    else:
        assert result.detail is None
        assert "detail" not in payload


@given(event=st.one_of(_events(), _execution_focused_events()))
@settings(max_examples=100, deadline=None)
def test_execution_id_appears_iff_provided(event: dict[str, object]) -> None:
    """The payload carries an executionId if and only if one was provided.

    The expected identifier is re-derived by the independent oracle; the
    notification field must equal it verbatim, and the ``executionId``
    payload key must exist exactly when the oracle finds one (Req 5.6).

    Args:
        event: Arbitrary or executionId-focused incident event.
    """
    expected = _provided_execution_id(event)
    record_coverage(
        "executionId provided" if expected is not None else "executionId absent"
    )

    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    assert result.execution_id == expected

    payload = result.to_payload()
    if expected is None:
        assert "executionId" not in payload
    else:
        assert payload["executionId"] == expected


@given(event=_events())
@settings(max_examples=100, deadline=None)
def test_normalize_is_deterministic(event: dict[str, object]) -> None:
    """The same arguments always produce an equal notification.

    Purity check: two calls with identical event, identity, and clock
    yield equal notifications and equal payloads.

    Args:
        event: Arbitrary EventBridge-like incident event.
    """
    first = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    second = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    assert first == second
    assert first.to_payload() == second.to_payload()


@st.composite
def _iso_moments(draw: st.DrawFn) -> tuple[str, datetime]:
    """Draw a parseable event time and the instant it denotes.

    Renders an instant either naive (denoting UTC per the documented
    rule) or in an arbitrary fixed whole-minute UTC offset, held inside
    the calendar so offset conversion cannot overflow.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        Pair of the ISO-8601 event time string and the aware datetime
        instant it denotes.
    """
    naive = draw(
        st.datetimes(
            # st.datetimes() requires NAIVE bounds by contract (the strategy
            # attaches timezones itself); the offset is applied explicitly
            # below, so these literals are deliberately tzinfo-free.
            min_value=datetime(1902, 1, 1),  # noqa: DTZ001
            max_value=datetime(2199, 12, 31),  # noqa: DTZ001
        )
    )
    offset_minutes = draw(st.one_of(st.none(), st.integers(-1439, 1439)))
    if offset_minutes is None:
        return naive.isoformat(), naive.replace(tzinfo=UTC)
    aware = naive.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return aware.isoformat(), aware


@given(moment=_iso_moments(), source=_source_values)
@settings(max_examples=100, deadline=None)
def test_parseable_event_time_is_preserved(
    moment: tuple[str, datetime], source: object
) -> None:
    """A parseable event time is echoed as the same instant in UTC.

    The notification timestamp parses to the exact instant the event's
    ``time`` denoted (naive values read as UTC) and is normalized to a
    zero UTC offset (Req 5.5).

    Args:
        moment: Pair of ISO event time text and the instant it denotes.
        source: Arbitrary top-level ``source`` value.
    """
    raw, instant = moment
    event: dict[str, object] = {"source": source, "time": raw}
    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    parsed = datetime.fromisoformat(result.timestamp)
    assert parsed == instant
    assert parsed.utcoffset() == timedelta(0)


@st.composite
def _events_without_usable_time(draw: st.DrawFn) -> dict[str, object]:
    """Draw events whose ``time`` cannot supply the timestamp.

    The ``time`` key is absent, ``None``, a non-string, or text that
    ``datetime.fromisoformat`` rejects.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        The event mapping handed to the normalizer.
    """
    event: dict[str, object] = {"source": draw(_source_values)}
    if draw(st.booleans()):
        event["detail"] = draw(st.one_of(st.none(), _json_values, _seeded_details()))
    shape = draw(st.integers(0, 4))
    if shape == 1:
        event["time"] = None
    elif shape == 2:
        event["time"] = draw(st.integers())
    elif shape == 3:
        event["time"] = draw(st.floats(allow_nan=False))
    elif shape == 4:
        event["time"] = draw(
            st.text(max_size=20).filter(lambda text: not _parses_as_isoformat(text))
        )
    return event


@given(event=_events_without_usable_time())
@settings(max_examples=100, deadline=None)
def test_unusable_event_time_falls_back_to_now_iso(event: dict[str, object]) -> None:
    """A missing or unparseable event time yields the injected clock.

    When ``time`` is absent, not a string, or not ISO-8601, the
    notification timestamp is the caller-injected ``now_iso`` verbatim,
    keeping the timestamp valid for every event (Req 5.5).

    Args:
        event: Incident event without a usable ``time`` value.
    """
    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    assert result.timestamp == _NOW_ISO


@st.composite
def _cloudwatch_cases(draw: st.DrawFn) -> tuple[dict[str, object], str, str]:
    """Draw a realistic CloudWatch Alarm event with its expected outcome.

    The alarm description mixes case-varied severity tokens with neutral
    noise words in shuffled order; the expected severity is the most
    severe token present, falling back to the state-value rule (``ALARM``
    means ``high``, anything else ``medium``) when no token occurs.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        Triple of the event, the expected severity, and the expected
        summary.
    """
    tokens = draw(st.lists(st.sampled_from(_SEVERITY_ORDER), unique=True, max_size=4))
    cased = [
        draw(st.sampled_from((token, token.upper(), token.capitalize())))
        for token in tokens
    ]
    noise = draw(st.lists(st.sampled_from(_NOISE_WORDS), max_size=4))
    description = " ".join(draw(st.permutations(cased + noise)))
    state_value = draw(
        st.sampled_from(("ALARM", "alarm", " Alarm ", "OK", "INSUFFICIENT_DATA", ""))
    )
    name = draw(_labels)
    reason = draw(_labels)
    event: dict[str, object] = {
        "source": "aws.cloudwatch",
        "detail": {
            "alarmName": name,
            "state": {"value": state_value, "reason": reason},
            "configuration": {"description": description},
        },
    }
    if tokens:
        severity = next(token for token in _SEVERITY_ORDER if token in tokens)
    elif state_value.strip().upper() == "ALARM":
        severity = "high"
    else:
        severity = "medium"
    return event, severity, f"{name}: {reason}"


@given(case=_cloudwatch_cases())
@settings(max_examples=100, deadline=None)
def test_cloudwatch_severity_token_precedence(
    case: tuple[dict[str, object], str, str],
) -> None:
    """CloudWatch severity scanning is case-insensitive, most severe wins.

    For any description built from severity tokens and noise, the
    notification severity equals the most severe token present (or the
    state-value fallback), and the summary joins the alarm name and the
    state reason (Req 5.5).

    Args:
        case: Triple of event, expected severity, and expected summary.
    """
    event, expected_severity, expected_summary = case
    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    assert result.source == "cloudwatch-alarm"
    assert result.severity == expected_severity
    assert result.summary == expected_summary


@st.composite
def _incident_manager_cases(draw: st.DrawFn) -> tuple[dict[str, object], str, str]:
    """Draw a realistic Incident Manager event with its expected outcome.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        Triple of the event, the expected severity mapped from the
        integer impact, and the expected summary (the incident title).
    """
    impact = draw(st.sampled_from(tuple(_IMPACT_SEVERITIES)))
    title = draw(_labels)
    event: dict[str, object] = {
        "source": "aws.ssm-incidents",
        "detail": {"title": title, "impact": impact},
    }
    return event, _IMPACT_SEVERITIES[impact], title


@given(case=_incident_manager_cases())
@settings(max_examples=100, deadline=None)
def test_incident_manager_impact_mapping(
    case: tuple[dict[str, object], str, str],
) -> None:
    """Incident Manager impact 1..5 maps to critical/high/medium/low/low.

    The notification severity follows the documented integer mapping and
    the summary preserves the incident title (Req 5.5).

    Args:
        case: Triple of event, expected severity, and expected summary.
    """
    event, expected_severity, expected_summary = case
    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    assert result.source == "incident-manager"
    assert result.severity == expected_severity
    assert result.summary == expected_summary


@st.composite
def _devops_cases(draw: st.DrawFn) -> tuple[dict[str, object], str, str]:
    """Draw a realistic DevOps Agent finding with its expected outcome.

    Recognized severities appear with case and surrounding-whitespace
    variations and normalize to their lowercase value; unrecognized
    values degrade to ``medium``. The summary comes from the finding
    ``summary``, falling back to ``title`` when it is absent or blank.

    Args:
        draw: Hypothesis draw function supplied by ``st.composite``.

    Returns:
        Triple of the event, the expected severity, and the expected
        summary.
    """
    base = draw(st.sampled_from(_SEVERITY_ORDER))
    padding = st.text(alphabet=" \t", max_size=3)
    severity_value: object
    if draw(st.booleans()):
        cased = draw(st.sampled_from((base, base.upper(), base.capitalize())))
        severity_value = f"{draw(padding)}{cased}{draw(padding)}"
        expected_severity = base
    else:
        severity_value = draw(
            st.sampled_from(("sev-1", "URGENT", "unknown", "", "critical!", None, 3))
        )
        expected_severity = "medium"
    title = draw(_labels)
    detail: dict[str, object] = {"severity": severity_value, "title": title}
    expected_summary = title
    if draw(st.booleans()):
        summary = draw(_labels)
        detail["summary"] = summary
        expected_summary = summary
    elif draw(st.booleans()):
        detail["summary"] = draw(st.sampled_from(("", "   ")))
    event: dict[str, object] = {
        "source": draw(st.sampled_from(("aws.aidevops", "custom.monitor"))),
        "detail": detail,
    }
    return event, expected_severity, expected_summary


@given(case=_devops_cases())
@settings(max_examples=100, deadline=None)
def test_devops_severity_normalization(
    case: tuple[dict[str, object], str, str],
) -> None:
    """DevOps Agent severities normalize case and whitespace or degrade.

    Recognized severity strings map to their lowercase value regardless
    of case and padding; anything else degrades to ``medium``; the
    summary preserves the finding text with the title fallback (Req 5.5).

    Args:
        case: Triple of event, expected severity, and expected summary.
    """
    event, expected_severity, expected_summary = case
    result = normalize(event, notification_id=_NOTIFICATION_ID, now_iso=_NOW_ISO)
    assert result.source == "devops-agent-finding"
    assert result.severity == expected_severity
    assert result.summary == expected_summary
