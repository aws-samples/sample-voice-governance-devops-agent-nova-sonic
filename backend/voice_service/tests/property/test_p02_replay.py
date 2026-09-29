# Feature: nova-sonic-support-portal, Property 2: Segmentation replay reconstructs context in order
"""Property test: segmentation replay reconstructs context in order.

**Validates: Requirements 2.3, 3.1**

For any conversation history (any lengths, roles, and unicode texts), the
replay-sequence builder emits exactly ``sessionStart``, then ``promptStart``
whose tool configuration contains the ``ask_devops_agent`` tool, then the
system-prompt block, then the history as text blocks whose role/text
sequence equals the original history in original chronological order,
before any audio event (Property 2).

The tool configuration is exercised both as the canonical
``ASK_DEVOPS_AGENT_TOOL_SPEC`` itself and as an arbitrary wrapper mapping
embedding it (for example ``{"tools": [spec]}``): the builder must carry
the passed mapping's content verbatim into ``promptStart.toolConfiguration``
(Req 2.3, "same tool configuration") so it always contains the
``ask_devops_agent`` tool (Req 3.1).
"""

from collections.abc import Mapping
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.segmentation import (
    ASK_DEVOPS_AGENT_TOOL_NAME,
    ASK_DEVOPS_AGENT_TOOL_SPEC,
    SYSTEM_ROLE,
    build_replay_sequence,
)
from app.domain.transcript import Role, TranscriptEntry


def _wrap_spec(extra: dict[str, str]) -> dict[str, object]:
    """Embed the canonical toolSpec in an arbitrary wrapper mapping.

    Args:
        extra: Arbitrary additional top-level keys the wrapper carries
            besides ``tools``.

    Returns:
        A ``{"tools": [ASK_DEVOPS_AGENT_TOOL_SPEC], **extra}`` mapping,
        mimicking callers that pass a whole ``toolConfiguration`` object
        rather than the bare spec.
    """
    wrapper: dict[str, object] = {"tools": [ASK_DEVOPS_AGENT_TOOL_SPEC]}
    wrapper.update(extra)
    return wrapper


_TOOL_CONFIGURATIONS: Final[st.SearchStrategy[Mapping[str, object]]] = st.one_of(
    st.just(ASK_DEVOPS_AGENT_TOOL_SPEC),
    st.dictionaries(
        keys=st.text().filter(lambda key: key != "tools"),
        values=st.text(),
        max_size=3,
    ).map(_wrap_spec),
)

_HISTORY_ENTRIES: Final[st.SearchStrategy[TranscriptEntry]] = st.builds(
    TranscriptEntry,
    seq=st.integers(0, 10_000),
    role=st.sampled_from(Role),
    text=st.text(),
    timestamp=st.text(min_size=1),
)


def _contains_ask_devops_agent(value: object) -> bool:
    """Search a JSON-shaped value recursively for the ask_devops_agent tool.

    Args:
        value: Any JSON-shaped value: a mapping, a list or tuple, or a
            scalar.

    Returns:
        ``True`` when some mapping reachable inside ``value`` carries a
        ``toolSpec`` whose ``name`` equals ``ASK_DEVOPS_AGENT_TOOL_NAME``,
        ``False`` otherwise.
    """
    if isinstance(value, Mapping):
        tool_spec = value.get("toolSpec")
        if isinstance(tool_spec, Mapping) and (
            tool_spec.get("name") == ASK_DEVOPS_AGENT_TOOL_NAME
        ):
            return True
        return any(_contains_ask_devops_agent(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_ask_devops_agent(item) for item in value)
    return False


@given(
    prompt_name=st.text(min_size=1),
    system_prompt=st.text(min_size=1),
    tool_configuration=_TOOL_CONFIGURATIONS,
    history=st.lists(_HISTORY_ENTRIES, max_size=30),
)
@settings(max_examples=100, deadline=None)
def test_replay_reconstructs_context_in_order(
    prompt_name: str,
    system_prompt: str,
    tool_configuration: Mapping[str, object],
    history: list[TranscriptEntry],
) -> None:
    """Replay is session start, prompt start, system block, then history.

    The event list is exactly ``sessionStart``, then ``promptStart`` whose
    ``toolConfiguration`` equals the passed mapping's content and contains
    the ``ask_devops_agent`` tool, then the system-prompt text block, then
    one text block per history entry whose role/text sequence equals the
    original history in original chronological order — with no audio event
    anywhere (every ``contentStart`` is TEXT and no ``audioInput`` exists),
    so the whole replay precedes any buffered audio the orchestrator
    flushes afterward.

    Args:
        prompt_name: Prompt identifier of the replacement stream; any
            non-empty unicode text.
        system_prompt: System-prompt text to replay; any non-empty unicode
            text.
        tool_configuration: The expiring stream's tool configuration —
            either the canonical spec or an arbitrary wrapper embedding it.
        history: Conversation history to replay, up to 30 entries with any
            roles, unicode texts, and sequence numbers.
    """
    events = build_replay_sequence(
        prompt_name=prompt_name,
        system_prompt=system_prompt,
        tool_configuration=tool_configuration,
        history=history,
    )

    # Exact shape: sessionStart + promptStart + (1 system + N history) triples.
    assert len(events) == 2 + 3 * (1 + len(history))

    # 1. sessionStart comes first.
    assert events[0]["type"] == "sessionStart"

    # 2. promptStart carries the passed tool configuration verbatim, which
    #    always contains the ask_devops_agent tool (Req 3.1).
    prompt_start = events[1]
    assert prompt_start["type"] == "promptStart"
    assert prompt_start["promptName"] == prompt_name
    tool_config = prompt_start["toolConfiguration"]
    assert tool_config == tool_configuration
    assert _contains_ask_devops_agent(tool_config)

    # 3. System-prompt block: contentStart (TEXT, SYSTEM) -> textInput
    #    carrying the system prompt -> contentEnd.
    system_start, system_text, system_end = events[2:5]
    assert system_start["type"] == "contentStart"
    assert system_start["promptName"] == prompt_name
    assert system_start["contentType"] == "TEXT"
    assert system_start["role"] == SYSTEM_ROLE
    assert system_text["type"] == "textInput"
    assert system_text["promptName"] == prompt_name
    assert system_text["content"] == system_prompt
    assert system_end["type"] == "contentEnd"
    assert system_end["promptName"] == prompt_name
    assert (
        system_start["contentName"]
        == system_text["contentName"]
        == system_end["contentName"]
    )

    # 4. Exactly len(history) consecutive text-block triples whose
    #    (role, text) sequence equals the original history in original
    #    chronological order (Req 2.3).
    history_events = events[5:]
    assert len(history_events) == 3 * len(history)
    observed: list[tuple[object, object]] = []
    for offset in range(0, len(history_events), 3):
        start, text_input, end = history_events[offset : offset + 3]
        assert start["type"] == "contentStart"
        assert start["promptName"] == prompt_name
        assert start["contentType"] == "TEXT"
        assert text_input["type"] == "textInput"
        assert text_input["promptName"] == prompt_name
        assert end["type"] == "contentEnd"
        assert end["promptName"] == prompt_name
        assert start["contentName"] == text_input["contentName"] == end["contentName"]
        observed.append((start["role"], text_input["content"]))
    assert observed == [(entry.role.value, entry.text) for entry in history]

    # 5. No audio event anywhere: the replay precedes any audio (Req 2.3).
    for event in events:
        assert event["type"] != "audioInput"
        if event["type"] == "contentStart":
            assert event["contentType"] == "TEXT"
