"""Session_Segmentation scheduling, replay building, and watchdog outcome.

Pure domain module behind the design's session segmentation state machine
(``domain.session``). A Bedrock_Stream is hard-capped by Bedrock at
8 minutes, so a per-stream timer triggers Session_Segmentation at 7 m 30 s
of elapsed stream age — safely before the cap (Req 2.2). Three concerns
live here:

- **Rollover scheduling**: :func:`should_rollover` decides whether a
  stream's age has reached the rollover threshold, and
  :func:`seconds_until_rollover` tells the segmentation timer how long to
  wait before the threshold is reached.
- **Replay building**: :func:`build_replay_sequence` reconstructs the
  conversation context for the replacement Bedrock_Stream as an ordered
  event list — exactly ``sessionStart``, then ``promptStart`` (same
  ``toolConfiguration``, including the ``ask_devops_agent`` tool), then
  the system-prompt text block, then the conversation history as
  USER/ASSISTANT text blocks in original chronological order — and emits
  no audio event, so every replayed event precedes the buffered audio the
  orchestrator flushes afterward (Req 2.3, design Property 2). Reconnect
  restoration (Req 8.3) replays persisted history through this same
  builder.
- **Watchdog outcome**: :class:`SegmentationOutcome` names the terminal
  result of one segmentation attempt and :func:`watchdog_expired` decides
  whether the 10-second completion budget has been exceeded (Req 2.6).

Replay events are JSON-ready plain structures (``dict``/``list``/scalar
values keyed by a ``"type"`` discriminator) that the Bedrock stream
adapter serializes into the wire-level Nova Sonic event grammar;
wire-only details such as ``textInputConfiguration`` or the
``interactive`` flag are the adapter's concern. Configuration mappings
passed in (or the module defaults) are deep-copied into the emitted
events, so no event aliases caller state or the exported constants.

This is a pure domain module: no I/O, no asyncio, no SDK imports, and no
clock reads — stream ages and instants are injected by the caller
(Req 17.6). Orchestration (the voice session manager) drives the actual
timers and raises the matching ``domain.session`` event:
``SEGMENTATION_DUE`` when :func:`should_rollover` fires,
``ROLLOVER_COMPLETE`` on ``SegmentationOutcome.COMPLETED``, and
``SEGMENTATION_FAILED`` on either failure outcome. The threshold and
budget defaults here mirror the configuration defaults
(``segmentation_rollover_seconds``, ``segmentation_watchdog_seconds`` in
``app.config``); the orchestrator passes the configured values in.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum, unique
from typing import Final

from app.domain.transcript import TranscriptEntry

__all__ = [
    "ASK_DEVOPS_AGENT_TOOL_NAME",
    "ASK_DEVOPS_AGENT_TOOL_SPEC",
    "AUDIO_OUTPUT_CONFIGURATION",
    "BEDROCK_STREAM_HARD_CAP_SECONDS",
    "DEFAULT_INFERENCE_CONFIGURATION",
    "ROLLOVER_STREAM_AGE_SECONDS",
    "SYSTEM_ROLE",
    "WATCHDOG_BUDGET_SECONDS",
    "ReplayEvent",
    "SegmentationOutcome",
    "build_replay_sequence",
    "build_text_input_block",
    "seconds_until_rollover",
    "should_rollover",
    "watchdog_expired",
]

BEDROCK_STREAM_HARD_CAP_SECONDS: Final = 8 * 60.0
"""Bedrock's hard maximum Bedrock_Stream duration: 8 minutes (480 s)."""

ROLLOVER_STREAM_AGE_SECONDS: Final = 7 * 60.0 + 30.0
"""Stream age that triggers Session_Segmentation: 7 m 30 s (450 s, Req 2.2)."""

WATCHDOG_BUDGET_SECONDS: Final = 10.0
"""Budget for a segmentation rollover to complete before it fails (Req 2.6)."""

ASK_DEVOPS_AGENT_TOOL_NAME: Final = "ask_devops_agent"
"""Name of the Nova Sonic tool that forwards requests to the DevOps Agent."""

# Exact stringified JSON schema from the design's toolSpec (kept verbatim,
# not rebuilt via json.dumps, so the wire bytes match the design document).
_ASK_DEVOPS_AGENT_INPUT_SCHEMA_JSON: Final = (
    '{"type":"object","properties":{"query":{"type":"string",'
    '"description":"The engineer\'s request, as text"}},"required":["query"]}'
)

ASK_DEVOPS_AGENT_TOOL_SPEC: Final[Mapping[str, object]] = {
    "toolSpec": {
        "name": ASK_DEVOPS_AGENT_TOOL_NAME,
        "description": (
            "Inspect the engineer's live AWS environment through the AWS "
            "DevOps Agent and return what it finds. Use this for every "
            "question about real state: instance ids and status, IAM roles "
            "and instance profiles, security group rules, VPC endpoints and "
            "routing, SSM agent status, alarms, logs, metrics, deployments, "
            "configuration, and root-cause analysis. Call it repeatedly to "
            "check several candidate causes."
        ),
        "inputSchema": {"json": _ASK_DEVOPS_AGENT_INPUT_SCHEMA_JSON},
    }
}
"""Canonical ``ask_devops_agent`` tool spec, verbatim from the design (Req 3.1).

Every Bedrock_Stream's ``promptStart`` carries a ``toolConfiguration``
containing this spec, and Session_Segmentation replays the same tool
configuration into the replacement stream (Req 2.3). Typed as a read-only
``Mapping``: treat it as deeply immutable and never mutate it.
"""

AUDIO_OUTPUT_CONFIGURATION: Final[Mapping[str, object]] = {
    "mediaType": "audio/lpcm",
    "sampleRateHertz": 24_000,
    "sampleSizeBits": 16,
    "channelCount": 1,
    "audioType": "SPEECH",
    "encoding": "base64",
    # REQUIRED by Nova Sonic. Omitting it makes the service reject promptStart
    # with "ValidationException: Received invalid voiceId: value must not be
    # empty" as soon as the stream's first output event is read, so every
    # session dies immediately. Verified against amazon.nova-2-sonic-v1:0:
    # absent -> ValidationException; "matthew" / "tiffany" / "amy" -> accepted.
    "voiceId": "matthew",
}
"""Default ``promptStart`` audio output configuration: 24 kHz 16-bit mono speech."""

DEFAULT_INFERENCE_CONFIGURATION: Final[Mapping[str, object]] = {
    "maxTokens": 1024,
    "topP": 0.9,
    "temperature": 0.7,
}
"""Default ``sessionStart`` inference configuration for Nova_Sonic."""

SYSTEM_ROLE: Final = "SYSTEM"
"""Role of the system-prompt text block; history blocks reuse ``Role`` values."""

_SYSTEM_CONTENT_NAME: Final = "replay-system"
"""Deterministic content name of the replayed system-prompt block."""

_HISTORY_CONTENT_NAME_PREFIX: Final = "replay-history-"
"""Prefix of history block content names; the 0-based position is appended."""

type ReplayEvent = dict[str, object]
"""One JSON-ready replay event, discriminated by its ``"type"`` key.

The ``"type"`` value is the Nova Sonic event name (``sessionStart``,
``promptStart``, ``contentStart``, ``textInput``, or ``contentEnd``); the
remaining keys are that event's payload, which the Bedrock stream adapter
serializes into the wire-level event grammar.
"""


def should_rollover(
    stream_age_seconds: float,
    threshold_seconds: float = ROLLOVER_STREAM_AGE_SECONDS,
) -> bool:
    """Decide whether a Bedrock_Stream's age requires Session_Segmentation.

    The rollover triggers as soon as the stream age reaches the threshold
    (``>=``), so a stream aged exactly 7 m 30 s rolls over — safely before
    Bedrock's 8-minute hard cap (Req 2.2).

    Args:
        stream_age_seconds: Elapsed age of the current Bedrock_Stream in
            seconds, measured by the caller's clock.
        threshold_seconds: Stream age that triggers the rollover; defaults
            to ``ROLLOVER_STREAM_AGE_SECONDS`` (450 s), mirroring the
            ``segmentation_rollover_seconds`` configuration default.

    Returns:
        ``True`` when ``stream_age_seconds`` is at or past the threshold,
        ``False`` while the stream may keep running.
    """
    return stream_age_seconds >= threshold_seconds


def seconds_until_rollover(
    stream_age_seconds: float,
    threshold_seconds: float = ROLLOVER_STREAM_AGE_SECONDS,
) -> float:
    """Compute how long the segmentation timer should still wait.

    Args:
        stream_age_seconds: Elapsed age of the current Bedrock_Stream in
            seconds, measured by the caller's clock.
        threshold_seconds: Stream age that triggers the rollover; defaults
            to ``ROLLOVER_STREAM_AGE_SECONDS`` (450 s).

    Returns:
        The remaining seconds until :func:`should_rollover` becomes
        ``True``; ``0.0`` when the threshold is already reached (never
        negative).
    """
    return max(0.0, threshold_seconds - stream_age_seconds)


def build_replay_sequence(
    *,
    prompt_name: str,
    system_prompt: str,
    tool_configuration: Mapping[str, object],
    history: Sequence[TranscriptEntry],
    inference_configuration: Mapping[str, object] = DEFAULT_INFERENCE_CONFIGURATION,
    audio_output_configuration: Mapping[str, object] = AUDIO_OUTPUT_CONFIGURATION,
) -> list[ReplayEvent]:
    """Build the exact ordered event list that replays a conversation.

    The returned sequence reconstructs context in a replacement
    Bedrock_Stream (Req 2.3, design Property 2) and is, in order:

    1. ``sessionStart`` carrying the inference configuration.
    2. ``promptStart`` carrying ``prompt_name``, the audio output
       configuration, and the ``toolConfiguration`` passed in — the same
       tool configuration as the expiring stream, including the
       ``ask_devops_agent`` tool (Req 3.1).
    3. The system-prompt text block: ``contentStart`` (TEXT, SYSTEM) →
       ``textInput`` → ``contentEnd``.
    4. Each history entry, in original chronological order, as a text
       block ``contentStart`` (TEXT, USER|ASSISTANT) → ``textInput`` →
       ``contentEnd``.

    No audio event is emitted: the orchestrator flushes the buffered
    audio FIFO into the new stream only after this sequence is delivered
    (Req 2.4). All configuration mappings are deep-copied, so the emitted
    events share no state with the caller or the module constants.

    Args:
        prompt_name: Prompt identifier of the replacement stream, carried
            by ``promptStart`` and every content-block event.
        system_prompt: System-prompt text establishing the read-only
            diagnostic persona (with incident context injected by the
            caller for incident-scoped sessions).
        tool_configuration: The expiring stream's ``toolConfiguration``,
            containing ``ASK_DEVOPS_AGENT_TOOL_SPEC`` (Req 2.3, 3.1).
        history: Conversation history to replay, in original chronological
            order — typically ``TranscriptAccumulator.entries()``.
        inference_configuration: ``sessionStart`` inference configuration;
            defaults to ``DEFAULT_INFERENCE_CONFIGURATION``.
        audio_output_configuration: ``promptStart`` audio output
            configuration; defaults to ``AUDIO_OUTPUT_CONFIGURATION``
            (24 kHz speech).

    Returns:
        The replay events in the exact order defined above, as JSON-ready
        plain structures for the Bedrock stream adapter to serialize.
    """
    events: list[ReplayEvent] = [
        {
            "type": "sessionStart",
            "inferenceConfiguration": _plain(inference_configuration),
        },
        {
            "type": "promptStart",
            "promptName": prompt_name,
            "audioOutputConfiguration": _plain(audio_output_configuration),
            "toolConfiguration": _plain(tool_configuration),
        },
    ]
    events.extend(
        _text_block(prompt_name, _SYSTEM_CONTENT_NAME, SYSTEM_ROLE, system_prompt)
    )
    for position, entry in enumerate(history):
        events.extend(
            _text_block(
                prompt_name,
                f"{_HISTORY_CONTENT_NAME_PREFIX}{position}",
                entry.role.value,
                entry.text,
            )
        )
    return events


@unique
class SegmentationOutcome(StrEnum):
    """Terminal result of one Session_Segmentation attempt (Req 2.6).

    The member values are the lowercase strings recorded in structured
    logs. Both failure members drive the same Req 2.6 handling in the
    orchestrator — persist the partial transcript, send a
    ``segmentation_failed`` error frame, log with the Voice_Session
    identifier — and map to the ``SEGMENTATION_FAILED`` state-machine
    event, while ``COMPLETED`` maps to ``ROLLOVER_COMPLETE``.
    """

    COMPLETED = "completed"
    """The replacement Bedrock_Stream went live within the watchdog budget."""

    FAILED = "failed"
    """The rollover failed outright, for example the new stream never opened."""

    TIMED_OUT = "timed_out"
    """The rollover did not complete within the 10-second watchdog budget."""

    @property
    def is_failure(self) -> bool:
        """Return whether this outcome triggers the Req 2.6 failure handling.

        Returns:
            ``True`` for ``FAILED`` and ``TIMED_OUT``, ``False`` for
            ``COMPLETED``.
        """
        return self is not SegmentationOutcome.COMPLETED


def watchdog_expired(
    started_at_seconds: float,
    now_seconds: float,
    budget_seconds: float = WATCHDOG_BUDGET_SECONDS,
) -> bool:
    """Decide whether a segmentation attempt has exceeded its watchdog budget.

    Req 2.6 fails a segmentation that "does not complete within
    10 seconds of being initiated": a rollover completing at exactly the
    budget boundary is still within it, so the watchdog expires only when
    the elapsed time strictly exceeds the budget. The orchestrator maps an
    expiry to ``SegmentationOutcome.TIMED_OUT``.

    Args:
        started_at_seconds: Instant the segmentation was initiated, in
            seconds on the caller's monotonic clock.
        now_seconds: Current instant, in seconds on the same clock.
        budget_seconds: Completion budget; defaults to
            ``WATCHDOG_BUDGET_SECONDS`` (10 s), mirroring the
            ``segmentation_watchdog_seconds`` configuration default.

    Returns:
        ``True`` when more than ``budget_seconds`` have elapsed since
        ``started_at_seconds``, ``False`` otherwise.
    """
    return now_seconds - started_at_seconds > budget_seconds


def build_text_input_block(
    *,
    prompt_name: str,
    content_name: str,
    role: str,
    text: str,
) -> list[ReplayEvent]:
    """Build one typed-text content block for a live stream (Req 1.1).

    Engineers can type a request alongside speaking it — identifiers like
    instance ids are easier to type than to pronounce — and a typed
    request enters the same conversation as a spoken one: as a USER text
    content block on the live stream. Structurally identical to a replayed
    history block, so the model sees no difference between a typed turn
    and a restored one.

    Args:
        prompt_name: Prompt identifier of the live stream, as carried by
            its ``promptStart`` event.
        content_name: Name identifying this content block; must be unique
            within the stream.
        role: Speaker of the block — a ``Role`` value, ``USER`` for a
            typed engineer request.
        text: The typed request text.

    Returns:
        The block's events in order: ``contentStart`` (TEXT, ``role``),
        ``textInput``, ``contentEnd`` — ready for
        ``BedrockStreamPort.send_event``.
    """
    return _text_block(prompt_name, content_name, role, text)


def _text_block(
    prompt_name: str,
    content_name: str,
    role: str,
    text: str,
) -> list[ReplayEvent]:
    """Build one replayed text content block as its three ordered events.

    Args:
        prompt_name: Prompt identifier of the replacement stream.
        content_name: Deterministic name identifying this content block.
        role: Speaker of the block: ``SYSTEM_ROLE`` for the system prompt,
            or a ``Role`` value (``USER``/``ASSISTANT``) for history.
        text: Text carried by the block's ``textInput`` event.

    Returns:
        The block's events in order: ``contentStart`` (TEXT, ``role``),
        ``textInput``, ``contentEnd``.
    """
    return [
        {
            "type": "contentStart",
            "promptName": prompt_name,
            "contentName": content_name,
            "contentType": "TEXT",
            "role": role,
        },
        {
            "type": "textInput",
            "promptName": prompt_name,
            "contentName": content_name,
            "content": text,
        },
        {
            "type": "contentEnd",
            "promptName": prompt_name,
            "contentName": content_name,
        },
    ]


def _plain(value: object) -> object:
    """Deep-copy a JSON-shaped value into plain ``dict``/``list`` structures.

    Guards the emitted events against aliasing: mappings (including
    read-only proxies) become plain ``dict`` instances and lists or tuples
    become plain ``list`` instances, so mutating an emitted event can
    never corrupt the caller's configuration or the exported module
    constants. Scalars are returned unchanged.

    Args:
        value: JSON-shaped value: a mapping, a list or tuple, or a scalar.

    Returns:
        An equivalent structure built only from plain ``dict``, ``list``,
        and scalar values, sharing no containers with the input.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
