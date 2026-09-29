"""Unit tests for DevOps Agent stream parsing (Req 3.3, 3.7).

Regression suite for the pure event-parsing helpers of
:mod:`app.adapters.devops_agent_client`, written against the shapes the
``devops-agent`` service model actually defines rather than guessed ones.

Why these exist: the adapter originally searched each event only for the
keys ``chunk``/``delta``/``content``/``text``/``bytes`` at its top level,
but a real answer fragment arrives as

    {"contentBlockDelta": {"delta": {"textDelta": {"text": "..."}}}}

so every fragment was skipped, every answer accumulated to the empty
string, and — because the tool router logged only failures — the portal
reported "I didn't get any results back" with nothing in the logs. These
tests pin the real shapes so that regression cannot recur silently.
"""

from typing import Final

import pytest

from app.adapters.devops_agent_client import (
    _extract_text,
    _raise_if_response_failed,
    _response_failure,
    _summary_text,
)
from app.exceptions import AgentRequestError

_FRAGMENT: Final = "Instance i-0abc123 is running."
"""Answer fragment carried by the content-block delta events below."""


def _text_delta_event(text: str) -> dict[str, object]:
    """Build the documented text-delta event shape.

    Args:
        text: Fragment carried by the delta.

    Returns:
        One ``contentBlockDelta`` event carrying a ``textDelta``.
    """
    return {
        "contentBlockDelta": {
            "index": 0,
            "delta": {"textDelta": {"text": text}},
            "sequenceNumber": 1,
        }
    }


def test_content_block_text_delta_is_extracted() -> None:
    """The real five-level delta shape yields its fragment."""
    assert _extract_text(_text_delta_event(_FRAGMENT)) == _FRAGMENT


def test_empty_text_delta_is_preserved_not_skipped() -> None:
    """An empty fragment is a value, not an absence (Req 3.3)."""
    assert _extract_text(_text_delta_event("")) == ""


def test_json_delta_is_not_answer_text() -> None:
    """``jsonDelta`` carries tool arguments, never prose for the engineer."""
    event: dict[str, object] = {
        "contentBlockDelta": {
            "index": 0,
            "delta": {"jsonDelta": {"partialJson": '{"region":"us-'}},
        },
    }
    assert _extract_text(event) is None


def test_lifecycle_events_carry_no_answer_text() -> None:
    """Lifecycle and keepalive events are skipped, not mistaken for text."""
    for event in (
        {"responseCreated": {"responseId": "r1"}},
        {"responseInProgress": {"responseId": "r1"}},
        {"contentBlockStart": {"index": 0, "type": "text", "id": "b1"}},
        {"contentBlockStop": {"index": 0}},
        {"responseCompleted": {"responseId": "r1"}},
        {"heartbeat": {"sequenceNumber": 9}},
    ):
        assert _extract_text(event) is None, event


def test_summary_event_is_read_separately_from_answer_text() -> None:
    """A ``summary`` is an action digest, used only as an answer fallback."""
    event = {"summary": {"content": "Checked the instance profile.", "sequenceNumber": 3}}
    assert _summary_text(event) == "Checked the instance profile."
    # Not answer prose: the worker holds summaries back unless nothing else
    # arrived, so the extractor must not report them as fragments.
    assert _extract_text(event) is None


def test_blank_summary_is_ignored() -> None:
    """A blank summary is no content at all."""
    assert _summary_text({"summary": {"content": ""}}) is None


def test_response_failed_is_surfaced_with_service_detail() -> None:
    """A failed response raises instead of silently yielding no text."""
    event = {
        "responseFailed": {
            "responseId": "r1",
            "errorCode": "ThrottlingException",
            "errorMessage": "Rate exceeded",
        }
    }
    assert _response_failure(event) == "ThrottlingException: Rate exceeded"
    with pytest.raises(AgentRequestError) as raised:
        _raise_if_response_failed(event)
    assert raised.value.operation == "SendMessage"
    assert raised.value.detail == "ThrottlingException: Rate exceeded"
    assert "Rate exceeded" in str(raised.value)


def test_response_failed_without_detail_still_raises() -> None:
    """A detail-less failure is still a failure, never a silent empty answer."""
    with pytest.raises(AgentRequestError):
        _raise_if_response_failed({"responseFailed": {"responseId": "r1"}})


def test_healthy_event_does_not_raise() -> None:
    """Events other than ``responseFailed`` pass through the failure check."""
    _raise_if_response_failed(_text_delta_event(_FRAGMENT))
    _raise_if_response_failed({"heartbeat": {}})


def test_forward_compatible_shapes_still_match_leniently() -> None:
    """An unknown-but-plausible shape is still mined for text."""
    assert _extract_text({"chunk": {"bytes": b"hello"}}) == "hello"
    assert _extract_text({"delta": {"text": "hi"}}) == "hi"
