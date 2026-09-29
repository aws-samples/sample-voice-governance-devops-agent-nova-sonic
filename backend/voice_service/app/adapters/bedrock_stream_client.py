"""Bedrock ``InvokeModelWithBidirectionalStream`` adapter for Nova Sonic.

Concrete ``BedrockStreamPort`` implementation
(``ports.bedrock_stream.BedrockStreamPort``) that carries one Nova Sonic
Bedrock_Stream over the Bedrock Runtime
``InvokeModelWithBidirectionalStream`` operation — an HTTP/2 bidirectional
event stream against the regional ``bedrock-runtime`` HTTPS endpoint
(Req 2.1). This module is the only place the Bedrock streaming SDK is
imported (Req 17.6, enforced by the import-linter contract in
``pyproject.toml``).

**SDK choice.** The GA path for bidirectional Bedrock streaming in Python
is the Smithy-based experimental ``aws_sdk_bedrock_runtime`` package (the
SDK used by the AWS Nova Sonic samples); ``boto3``/``aioboto3`` do not
support HTTP/2 bidirectional streaming. The package is pre-1.0, so it is
pinned to a compatible-release line in ``pyproject.toml`` and every SDK
touchpoint is confined to small private helpers (``_invoke_stream``,
``_input_chunk``, ``_output_chunk_bytes``, ``_SDK_TRANSPORT_ERRORS``) so a
future SDK swap only touches this file. Credentials come from the SDK's
default resolver chain (environment → profile → ECS container credentials
→ IMDS), which serves the Fargate task role in deployment.

**Encryption in transit (Req 12.7, 12.8).** The SDK's endpoint resolver
only produces the ``https://bedrock-runtime.<region>.amazonaws.com``
endpoint, so every connection is TLS; no plaintext transport exists. When
a TLS connection cannot be established, the failure surfaces as
``StreamOpenError`` (opening) or ``BedrockStreamError`` (established
stream) and no data is transmitted — there is no unencrypted fallback.

**Wire format.** Nova Sonic input and output events are JSON envelopes
``{"event": {"<eventName>": {...payload...}}}`` carried as the raw bytes
of bidirectional payload parts. :func:`encode_input_event` converts the
JSON-ready domain ``ReplayEvent`` mappings (``{"type": <eventName>,
...payload}``, built by ``domain.segmentation``) into wire envelopes, and
:func:`decode_output_chunk` flattens received envelopes back into the
same ``"type"``-discriminated shape the port contract promises. Two
conventions bridge the domain and wire vocabularies:

- The domain uses ``"contentType"`` for a content block's kind (TEXT /
  AUDIO / TOOL) because ``"type"`` is the domain-side event
  discriminator; the wire uses ``"type"`` for both. Encoding renames
  ``contentType`` → ``type`` inside the payload, and decoding renames a
  payload-level ``type`` → ``contentType``, so the discriminator is never
  clobbered in either direction.
- Wire-only configuration blocks the domain deliberately omits are
  defaulted here: ``promptStart`` gains ``textOutputConfiguration`` and
  ``toolUseOutputConfiguration``; a TEXT ``contentStart`` gains
  ``interactive`` (``False`` — the documented value for system prompts
  and replayed history) and ``textInputConfiguration``; an AUDIO
  ``contentStart`` gains ``interactive`` (``True``) and the 16 kHz
  ``audioInputConfiguration`` (Req 1.1). Explicit keys in the domain
  event always win over these defaults.
- ``promptStart``'s ``toolConfiguration`` is normalized to the wire's
  ``{"tools": [...]}`` shape: a mapping already carrying a ``"tools"``
  key passes through verbatim, while a bare ``{"toolSpec": {...}}``
  mapping (the shape of ``ASK_DEVOPS_AGENT_TOOL_SPEC``, Req 3.1) is
  wrapped as ``{"tools": [spec]}``; see
  :func:`_normalize_tool_configuration`.

Both directions pass unknown event names through untouched (decoded
events keep their wire type name, e.g. ``usageEvent``), keeping the
adapter forward-compatible with grammar additions.

Failures raise only the ``BedrockStreamError`` branch of the portal
hierarchy: ``StreamOpenError`` when the stream cannot be established or
the initial replay sequence cannot be delivered (Req 1.6), and
``BedrockStreamError`` for failures on an established stream. Chained
causes (``raise ... from``) preserve the underlying SDK diagnostics.
"""

import base64
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
    InvokeModelWithBidirectionalStreamOperationInput,
    InvokeModelWithBidirectionalStreamOutputChunk,
)
from awscrt.exceptions import AwsCrtError
from smithy_core.aio.eventstream import DuplexEventStream
from smithy_core.aio.interfaces.eventstream import EventReceiver
from smithy_core.exceptions import SmithyError

from app.domain.segmentation import ReplayEvent
from app.exceptions import BedrockStreamError, StreamOpenError
from app.ports.bedrock_stream import BedrockStreamPort

__all__ = [
    "AUDIO_INPUT_CONFIGURATION",
    "DEFAULT_TEXT_OUTPUT_CONFIGURATION",
    "DEFAULT_TOOL_USE_OUTPUT_CONFIGURATION",
    "TEXT_INPUT_CONFIGURATION",
    "BedrockStreamClient",
    "decode_output_chunk",
    "encode_input_event",
]

TEXT_INPUT_CONFIGURATION: Final[Mapping[str, object]] = {"mediaType": "text/plain"}
"""``textInputConfiguration`` defaulted onto TEXT ``contentStart`` events."""

DEFAULT_TEXT_OUTPUT_CONFIGURATION: Final[Mapping[str, object]] = {
    "mediaType": "text/plain"
}
"""``textOutputConfiguration`` defaulted onto ``promptStart`` events."""

DEFAULT_TOOL_USE_OUTPUT_CONFIGURATION: Final[Mapping[str, object]] = {
    "mediaType": "application/json"
}
"""``toolUseOutputConfiguration`` defaulted onto ``promptStart`` events."""

AUDIO_INPUT_CONFIGURATION: Final[Mapping[str, object]] = {
    "mediaType": "audio/lpcm",
    "sampleRateHertz": 16_000,
    "sampleSizeBits": 16,
    "channelCount": 1,
    "audioType": "SPEECH",
    "encoding": "base64",
}
"""``audioInputConfiguration`` defaulted onto AUDIO ``contentStart`` events.

16 kHz 16-bit mono LPCM, base64-encoded — the engineer microphone format
the Frontend streams (Req 1.1).
"""

# Wire and domain key vocabulary shared by the encode and decode helpers.
_EVENT_KEY: Final = "event"
_TYPE_KEY: Final = "type"
_CONTENT_TYPE_KEY: Final = "contentType"
_PROMPT_NAME_KEY: Final = "promptName"
_PROMPT_START_EVENT: Final = "promptStart"
_TOOL_CONFIGURATION_KEY: Final = "toolConfiguration"
_TOOLS_KEY: Final = "tools"
_TOOL_SPEC_KEY: Final = "toolSpec"

# Exception detail phrases, defined as constants so raise sites pass typed
# context instead of formatting message strings (codebase convention).
_ALREADY_OPENED_DETAIL: Final = "stream instance already opened or closed"
_ENCODE_FAILED_DETAIL: Final = "initial replay sequence failed to encode"
_NO_OUTPUT_STREAM_DETAIL: Final = "service returned no output stream"
_NOT_OPEN_DETAIL: Final = "stream is not open"
_SEND_FAILED_DETAIL: Final = "send on live stream failed"
_RECEIVE_FAILED_DETAIL: Final = "receive on live stream failed"
_TEARDOWN_FAILED_DETAIL: Final = "graceful teardown handshake failed"
_CLOSE_FAILED_DETAIL: Final = "closing the underlying stream failed"
_MISSING_TYPE_DETAIL: Final = "input event has no 'type' discriminator"
_UNSERIALIZABLE_EVENT_DETAIL: Final = "input event is not JSON-serializable"
_UNDECODABLE_CHUNK_DETAIL: Final = "output chunk is not valid JSON"
_ERROR_EVENT_DETAIL: Final = "service reported a stream error event"

# The specific exception classes the SDK surface can raise: SmithyError
# covers the SDK's own hierarchy (config, endpoint, serialization, and
# modeled service errors), AwsCrtError covers the CRT HTTP/2 transport,
# and OSError covers socket-level failures beneath both. Never a bare
# Exception (Req 17.5).
_SDK_TRANSPORT_ERRORS: Final = (SmithyError, AwsCrtError, OSError)


def encode_input_event(event: ReplayEvent) -> bytes:
    """Encode one domain input event into its Nova Sonic wire envelope.

    Converts the ``"type"``-discriminated ``ReplayEvent`` mapping into the
    UTF-8 JSON bytes of ``{"event": {"<type>": {...payload...}}}``. The
    payload is the event minus its ``"type"`` key, with the domain
    ``"contentType"`` key renamed to the wire's ``"type"``, the
    wire-only defaults applied (``promptStart`` output configurations,
    TEXT/AUDIO ``contentStart`` input configurations and ``interactive``
    flags), and ``promptStart``'s ``toolConfiguration`` normalized to
    the wire's ``{"tools": [...]}`` shape; explicit keys in ``event``
    always win over defaults. Pure function — no I/O and no SDK types —
    so the wire format is testable without a network.

    Args:
        event: One JSON-ready input event in the ``ReplayEvent`` shape
            emitted by ``domain.segmentation`` (or built by the caller for
            audio and tool-result blocks), discriminated by its ``"type"``
            key.

    Returns:
        The UTF-8 encoded JSON wire envelope for the event.

    Raises:
        BedrockStreamError: If ``event`` lacks a non-empty string
            ``"type"`` key, or its payload cannot be serialized to JSON.
    """
    payload: dict[str, object] = dict(event)
    event_name = payload.pop(_TYPE_KEY, None)
    if not isinstance(event_name, str) or not event_name:
        raise BedrockStreamError(_MISSING_TYPE_DETAIL)
    if _CONTENT_TYPE_KEY in payload:
        payload[_TYPE_KEY] = payload.pop(_CONTENT_TYPE_KEY)
    _apply_wire_defaults(event_name, payload)
    try:
        rendered = json.dumps({_EVENT_KEY: {event_name: payload}}, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise BedrockStreamError(_UNSERIALIZABLE_EVENT_DETAIL) from exc
    return rendered.encode("utf-8")


def decode_output_chunk(payload: bytes) -> dict[str, object] | None:
    """Decode one Nova Sonic output chunk into a port-contract event.

    Parses the chunk bytes as JSON and flattens the
    ``{"event": {"<name>": {...payload...}}}`` envelope into
    ``{"type": "<name>", ...payload}``. A payload-level ``"type"`` field
    (present on output ``contentStart``/``contentEnd`` events, where it
    names the block kind) is renamed to ``"contentType"`` first, so the
    event-name discriminator is never clobbered. Payload fields keep
    their wire values — ``audioOutput`` content stays base64 text — and
    every event name passes through unchanged (``completionStart``,
    ``textOutput``, ``audioOutput``, ``toolUse``, ``contentEnd``,
    ``completionEnd``, plus forward-compatible names such as
    ``usageEvent``). Pure function, testable without a network.

    Args:
        payload: Raw bytes of one bidirectional output payload part.

    Returns:
        The flattened event mapping, or ``None`` for valid JSON that is
        not a single-event envelope (for example keep-alive or unknown
        framing objects), which the caller skips.

    Raises:
        BedrockStreamError: If ``payload`` is not valid JSON (undecodable
            bytes are a stream-level protocol failure, not a skippable
            frame).
    """
    try:
        parsed: object = json.loads(payload)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise BedrockStreamError(_UNDECODABLE_CHUNK_DETAIL) from exc
    if not isinstance(parsed, dict):
        return None
    envelope = parsed.get(_EVENT_KEY)
    if not isinstance(envelope, dict) or len(envelope) != 1:
        return None
    event_name, body = next(iter(envelope.items()))
    if not isinstance(event_name, str) or not isinstance(body, dict):
        return None
    flattened: dict[str, object] = dict(body)
    if _TYPE_KEY in flattened:
        flattened[_CONTENT_TYPE_KEY] = flattened.pop(_TYPE_KEY)
    flattened[_TYPE_KEY] = event_name
    return flattened


def _apply_wire_defaults(event_name: str, payload: dict[str, object]) -> None:
    """Apply the wire-only configuration defaults for one event payload.

    Fills in the Nova Sonic grammar fields the domain deliberately leaves
    to the adapter, without overriding keys the event already carries:
    ``promptStart`` gains the text and tool-use output configurations and
    has its ``toolConfiguration`` normalized to the wire's
    ``{"tools": [...]}`` shape via :func:`_normalize_tool_configuration`;
    a TEXT ``contentStart`` gains ``interactive: False`` (the documented
    value for system prompts and replayed history) and the ``text/plain``
    input configuration; an AUDIO ``contentStart`` gains
    ``interactive: True`` and the 16 kHz LPCM input configuration. All
    other event types are left untouched.

    Args:
        event_name: Wire event name popped from the domain event's
            ``"type"`` key.
        payload: Mutable wire payload (``contentType`` already renamed to
            ``type``); modified in place.
    """
    if event_name == _PROMPT_START_EVENT:
        payload.setdefault(
            "textOutputConfiguration", dict(DEFAULT_TEXT_OUTPUT_CONFIGURATION)
        )
        payload.setdefault(
            "toolUseOutputConfiguration", dict(DEFAULT_TOOL_USE_OUTPUT_CONFIGURATION)
        )
        tool_configuration = payload.get(_TOOL_CONFIGURATION_KEY)
        if tool_configuration is not None:
            payload[_TOOL_CONFIGURATION_KEY] = _normalize_tool_configuration(
                tool_configuration
            )
        return
    if event_name == "contentStart":
        block_type = payload.get(_TYPE_KEY)
        if block_type == "TEXT":
            payload.setdefault("interactive", False)
            payload.setdefault(
                "textInputConfiguration", dict(TEXT_INPUT_CONFIGURATION)
            )
        elif block_type == "AUDIO":
            payload.setdefault("interactive", True)
            payload.setdefault(
                "audioInputConfiguration", dict(AUDIO_INPUT_CONFIGURATION)
            )


def _normalize_tool_configuration(tool_configuration: object) -> object:
    """Normalize a ``promptStart`` tool configuration into its wire shape.

    The wire grammar's ``toolConfiguration`` is ``{"tools": [<toolSpec
    mapping>, ...]}``, but the domain deliberately stays wire-agnostic:
    ``domain.segmentation.ASK_DEVOPS_AGENT_TOOL_SPEC`` is the bare
    ``{"toolSpec": {...}}`` mapping, and ``build_replay_sequence`` passes
    whatever ``tool_configuration`` it was given straight through. The
    shape is disambiguated by inspection:

    - a mapping with a ``"tools"`` key is already wire-shaped and passes
      through verbatim;
    - a mapping with a ``"toolSpec"`` key is a single bare tool spec and
      is wrapped as ``{"tools": [<mapping>]}``;
    - anything else (unknown mapping shapes or non-mappings) passes
      through verbatim, leaving forward-compatible or caller-custom
      shapes untouched.

    Args:
        tool_configuration: The ``toolConfiguration`` value carried by
            the domain's ``promptStart`` event.

    Returns:
        The wire-shaped tool configuration: ``{"tools": [...]}`` for a
        bare tool spec, the input unchanged otherwise.
    """
    if isinstance(tool_configuration, Mapping):
        if _TOOLS_KEY in tool_configuration:
            return tool_configuration
        if _TOOL_SPEC_KEY in tool_configuration:
            return {_TOOLS_KEY: [dict(tool_configuration)]}
    return tool_configuration


def _find_prompt_name(events: Sequence[ReplayEvent]) -> str | None:
    """Extract the prompt name carried by a sequence's ``promptStart`` event.

    Args:
        events: Input events in delivery order, as passed to
            :meth:`BedrockStreamClient.open`.

    Returns:
        The ``promptName`` of the first ``promptStart`` event whose value
        is a non-empty string, or ``None`` when the sequence carries none.
    """
    for event in events:
        if event.get(_TYPE_KEY) == _PROMPT_START_EVENT:
            prompt_name = event.get(_PROMPT_NAME_KEY)
            if isinstance(prompt_name, str) and prompt_name:
                return prompt_name
    return None


def _input_chunk(payload: bytes) -> InvokeModelWithBidirectionalStreamInputChunk:
    """Wrap wire-envelope bytes into the SDK's input chunk shape.

    SDK-specific seam: the only place input payload bytes meet the SDK's
    event-stream model types.

    Args:
        payload: UTF-8 JSON wire envelope produced by
            :func:`encode_input_event`.

    Returns:
        The chunk ready for the duplex stream's input publisher.
    """
    return InvokeModelWithBidirectionalStreamInputChunk(
        value=BidirectionalInputPayloadPart(bytes_=payload)
    )


def _output_chunk_bytes(member: object) -> bytes | None:
    """Extract the payload bytes from one output event-stream member.

    SDK-specific seam for the receive path: chunk members yield their
    payload bytes; modeled error members (the service's in-stream
    ``internalServerException`` / ``validationException`` /
    ``throttlingException`` / ``modelStreamErrorException`` /
    ``modelTimeoutException`` / ``serviceUnavailableException`` variants,
    whose ``value`` is an exception instance) fail the stream; anything
    else — including unknown future union variants — is skipped.

    Args:
        member: One member of the SDK's output event union.

    Returns:
        The chunk's payload bytes, or ``None`` when the member carries no
        decodable payload (empty chunk or unknown variant).

    Raises:
        BedrockStreamError: If the member is a modeled stream error event,
            chaining the service exception as the cause.
    """
    if isinstance(member, InvokeModelWithBidirectionalStreamOutputChunk):
        part = getattr(member, "value", None)
        payload = getattr(part, "bytes_", None)
        return payload if isinstance(payload, bytes) else None
    value = getattr(member, "value", None)
    if isinstance(value, Exception):
        raise BedrockStreamError(_ERROR_EVENT_DETAIL) from value
    return None


def _error_detail(exc: Exception) -> str:
    """Render a short diagnostic detail for a caught SDK exception.

    Args:
        exc: The underlying SDK, transport, or socket exception.

    Returns:
        ``"<ExceptionClassName>: <message>"``, suitable for the ``detail``
        of ``StreamOpenError`` (the full exception stays chained via
        ``raise ... from``).
    """
    return f"{type(exc).__name__}: {exc}"


async def _close_quietly(stream: DuplexEventStream[Any, Any, Any]) -> None:
    """Close a duplex stream, suppressing transport errors.

    Used on failure paths where the stream is already known broken and a
    close failure would mask the original error being raised.

    Args:
        stream: The half-open or failed duplex event stream to close.
    """
    with contextlib.suppress(*_SDK_TRANSPORT_ERRORS):
        await stream.close()


class BedrockStreamClient(BedrockStreamPort):
    """One Nova Sonic Bedrock_Stream over ``InvokeModelWithBidirectionalStream``.

    Concrete adapter behind ``BedrockStreamPort``: opens the HTTP/2
    bidirectional stream against the regional Bedrock Runtime HTTPS
    endpoint (Req 2.1, 12.7), serializes domain events into the Nova
    Sonic wire grammar, and decodes output chunks back into the port's
    ``"type"``-discriminated mappings. One instance represents exactly
    one Bedrock_Stream lifecycle; segmentation rollovers and reconnect
    replays construct a fresh instance per replacement stream (Req 2.2,
    8.3).

    The model identifier and region are injected by the application
    wiring from validated configuration — the deployment targets
    us-east-1 (Req 2.1) — and are never hardcoded here (Req 14.2). The
    prompt name observed in the opening sequence's ``promptStart`` event
    is tracked so :meth:`close` can perform the graceful ``promptEnd`` →
    ``sessionEnd`` teardown handshake (Req 2.5).
    """

    def __init__(self, model_id: str, region: str) -> None:
        """Initialize an unopened stream adapter.

        Args:
            model_id: Bedrock model identifier of the Nova Sonic model to
                invoke, from configuration (``NOVA_SONIC_MODEL_ID``).
            region: AWS region hosting the Bedrock Runtime endpoint, from
                configuration; us-east-1 in this deployment (Req 2.1).
        """
        self._model_id = model_id
        self._region = region
        self._client: AsyncBedrockRuntimeClient | None = None
        self._stream: DuplexEventStream[Any, Any, Any] | None = None
        self._output_stream: EventReceiver[Any] | None = None
        self._prompt_name: str | None = None
        self._opened = False
        self._closed = False

    async def open(self, events: Sequence[ReplayEvent]) -> None:
        """Establish the stream and deliver the initial event sequence.

        Encodes every replay event up front, invokes
        ``InvokeModelWithBidirectionalStream`` for the configured model,
        sends the events in order, and then awaits the service's initial
        response — so a rejected stream (bad model access, authentication
        failure, TLS failure) surfaces here rather than on first use.
        The context is therefore fully reconstructed before any audio
        flows (Req 2.3), and the ``promptStart`` prompt name is recorded
        for the eventual teardown handshake.

        Args:
            events: Initial input events in the exact order to deliver,
                as built by ``domain.segmentation.build_replay_sequence``
                (``sessionStart`` → ``promptStart`` carrying the
                ``ask_devops_agent`` tool configuration, Req 3.1 → system
                prompt → history blocks).

        Raises:
            StreamOpenError: If this instance was already opened or
                closed, the events cannot be encoded, the stream cannot
                be established, or the initial sequence cannot be
                delivered (Req 1.6).
        """
        if self._opened or self._closed:
            raise StreamOpenError(_ALREADY_OPENED_DETAIL)
        try:
            payloads = [encode_input_event(event) for event in events]
        except BedrockStreamError as exc:
            raise StreamOpenError(_ENCODE_FAILED_DETAIL) from exc
        try:
            client, stream = await self._invoke_stream()
        except _SDK_TRANSPORT_ERRORS as exc:
            raise StreamOpenError(_error_detail(exc)) from exc
        try:
            for payload in payloads:
                await stream.input_stream.send(_input_chunk(payload))
            _, output_stream = await stream.await_output()
        except _SDK_TRANSPORT_ERRORS as exc:
            await _close_quietly(stream)
            raise StreamOpenError(_error_detail(exc)) from exc
        if output_stream is None:
            await _close_quietly(stream)
            raise StreamOpenError(_NO_OUTPUT_STREAM_DETAIL)
        self._client = client
        self._stream = stream
        self._output_stream = output_stream
        self._prompt_name = _find_prompt_name(events)
        self._opened = True

    async def send_event(self, event: ReplayEvent) -> None:
        """Send one input event into the live stream.

        Serializes the domain event into its wire envelope and sends it.
        A ``promptStart`` sent through here updates the tracked prompt
        name used by the teardown handshake.

        Args:
            event: One JSON-ready input event discriminated by its
                ``"type"`` key, in the ``ReplayEvent`` shape emitted by
                ``domain.segmentation``.

        Raises:
            BedrockStreamError: If the stream is not open, the event
                cannot be encoded, or the send fails on the live stream.
        """
        stream = self._require_open()
        payload = encode_input_event(event)
        if event.get(_TYPE_KEY) == _PROMPT_START_EVENT:
            prompt_name = event.get(_PROMPT_NAME_KEY)
            if isinstance(prompt_name, str) and prompt_name:
                self._prompt_name = prompt_name
        await self._send_wire(stream, payload)

    async def send_audio(
        self, prompt_name: str, content_name: str, pcm: bytes
    ) -> None:
        """Send one ``audioInput`` event carrying raw engineer audio.

        Base64-encodes the PCM bytes into the event's ``content`` per the
        wire grammar (Req 1.2); the enclosing AUDIO content block is
        opened and closed by the caller via :meth:`send_event`.

        Args:
            prompt_name: Prompt identifier of the live stream, as carried
                by its ``promptStart`` event.
            content_name: Name of the currently open audio content block
                the audio belongs to.
            pcm: Raw 16 kHz 16-bit mono PCM audio bytes exactly as
                received from the client (Req 1.1).

        Raises:
            BedrockStreamError: If the stream is not open or the send
                fails on the live stream.
        """
        stream = self._require_open()
        event: ReplayEvent = {
            "type": "audioInput",
            "promptName": prompt_name,
            "contentName": content_name,
            "content": base64.b64encode(pcm).decode("ascii"),
        }
        await self._send_wire(stream, encode_input_event(event))

    async def send_tool_result(
        self, prompt_name: str, tool_use_id: str, content: str
    ) -> None:
        """Send one complete tool-result content block.

        Emits the wire grammar's three-event TOOL block — ``contentStart``
        (``type: TOOL``, ``role: TOOL``, ``interactive: false``, with the
        ``toolResultInputConfiguration`` referencing ``tool_use_id``) →
        ``toolResult`` (the stringified JSON payload) → ``contentEnd`` —
        returning the ``ask_devops_agent`` answer or error indication to
        Nova_Sonic (Req 3.4, 3.7). The block's content name is a fresh
        unique identifier as the grammar requires.

        Args:
            prompt_name: Prompt identifier of the live stream, as carried
                by its ``promptStart`` event.
            tool_use_id: The ``toolUseId`` from the ``toolUse`` output
                event this result answers.
            content: The tool-result payload as stringified JSON —
                ``{"answer": "..."}`` on success or ``{"error": "..."}``
                on failure or refusal.

        Raises:
            BedrockStreamError: If the stream is not open or a send fails
                on the live stream.
        """
        stream = self._require_open()
        content_name = f"tool-result-{uuid.uuid4().hex}"
        block: list[ReplayEvent] = [
            {
                "type": "contentStart",
                "promptName": prompt_name,
                "contentName": content_name,
                "interactive": False,
                "contentType": "TOOL",
                "role": "TOOL",
                "toolResultInputConfiguration": {
                    "toolUseId": tool_use_id,
                    "type": "TEXT",
                    "textInputConfiguration": dict(TEXT_INPUT_CONFIGURATION),
                },
            },
            {
                "type": "toolResult",
                "promptName": prompt_name,
                "contentName": content_name,
                "content": content,
            },
            {
                "type": "contentEnd",
                "promptName": prompt_name,
                "contentName": content_name,
            },
        ]
        for event in block:
            await self._send_wire(stream, encode_input_event(event))

    async def receive(self) -> AsyncIterator[Mapping[str, object]]:
        """Yield the stream's decoded output events as they arrive.

        Iterates the SDK's output event stream, extracts each chunk's
        payload bytes, and yields the flattened event mappings produced
        by :func:`decode_output_chunk` — ``audioOutput`` content stays
        base64 text per the port contract (Req 1.3, 1.4). Iteration ends
        when the service completes the stream, including after the
        graceful teardown performed by :meth:`close`.

        Yields:
            Decoded output events, each a JSON-ready mapping
            discriminated by its ``"type"`` key (``completionStart``,
            ``contentStart``, ``textOutput``, ``audioOutput``,
            ``toolUse``, ``contentEnd``, ``completionEnd``, and any
            forward-compatible event names such as ``usageEvent``).

        Raises:
            BedrockStreamError: If the stream is not open, the live
                stream fails mid-iteration, the service reports an
                in-stream error event, or a chunk is undecodable.
        """
        output_stream = self._output_stream
        if output_stream is None:
            raise BedrockStreamError(_NOT_OPEN_DETAIL)
        try:
            async for member in output_stream:
                payload = _output_chunk_bytes(member)
                if payload is None:
                    continue
                decoded = decode_output_chunk(payload)
                if decoded is not None:
                    yield decoded
        except _SDK_TRANSPORT_ERRORS as exc:
            raise BedrockStreamError(_RECEIVE_FAILED_DETAIL) from exc

    async def close(self) -> None:
        """Tear the stream down gracefully; repeated calls are no-ops.

        On the first call against an opened stream, performs the wire
        grammar's teardown handshake — ``promptEnd`` for the tracked
        prompt name, then ``sessionEnd`` — and closes both directions of
        the underlying HTTP/2 stream (Req 2.5). The instance is marked
        closed before the handshake starts, so a second call is a no-op
        even if the first failed; closing a never-opened instance does
        nothing.

        Raises:
            BedrockStreamError: If the teardown handshake or the
                underlying stream close fails on a live stream (the
                underlying stream is still closed on a best-effort basis
                when the handshake fails).
        """
        if self._closed:
            return
        self._closed = True
        stream = self._stream
        if stream is None:
            return
        handshake_error: BedrockStreamError | None = None
        try:
            if self._prompt_name is not None:
                await self._send_wire(
                    stream,
                    encode_input_event(
                        {"type": "promptEnd", "promptName": self._prompt_name}
                    ),
                )
            await self._send_wire(stream, encode_input_event({"type": "sessionEnd"}))
        except BedrockStreamError as exc:
            handshake_error = exc
        try:
            await stream.close()
        except _SDK_TRANSPORT_ERRORS as exc:
            if handshake_error is None:
                raise BedrockStreamError(_CLOSE_FAILED_DETAIL) from exc
        if handshake_error is not None:
            raise handshake_error

    async def _invoke_stream(
        self,
    ) -> tuple[AsyncBedrockRuntimeClient, DuplexEventStream[Any, Any, Any]]:
        """Resolve SDK configuration and start the bidirectional operation.

        SDK-specific seam for the open path: resolves the client
        configuration for the injected region (credentials via the SDK's
        default chain, TLS-only regional endpoint, Req 12.7) and invokes
        ``InvokeModelWithBidirectionalStream`` for the injected model.

        Returns:
            The constructed client (kept alive for the stream's lifetime)
            and the duplex event stream.

        Raises:
            SmithyError: If configuration resolution, endpoint
                resolution, or the operation invocation fails; translated
                by the caller into ``StreamOpenError``.
            AwsCrtError: If the CRT HTTP/2 transport fails to connect;
                translated by the caller into ``StreamOpenError``.
            OSError: If the connection fails at the socket level;
                translated by the caller into ``StreamOpenError``.
        """
        config = await AsyncBedrockRuntimeConfig.resolve(region=self._region)
        client = AsyncBedrockRuntimeClient(config=config)
        stream = await client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self._model_id)
        )
        return client, stream

    def _require_open(self) -> DuplexEventStream[Any, Any, Any]:
        """Return the live duplex stream, rejecting unopened or closed states.

        Returns:
            The duplex event stream established by :meth:`open`.

        Raises:
            BedrockStreamError: If :meth:`open` has not completed
                successfully or :meth:`close` was already called.
        """
        if self._stream is None or self._closed:
            raise BedrockStreamError(_NOT_OPEN_DETAIL)
        return self._stream

    async def _send_wire(
        self, stream: DuplexEventStream[Any, Any, Any], payload: bytes
    ) -> None:
        """Send one encoded wire envelope on the stream's input publisher.

        SDK-specific seam for the send path: wraps the payload bytes in
        the SDK chunk shape and translates transport failures into the
        portal hierarchy.

        Args:
            stream: The duplex event stream to send on.
            payload: UTF-8 JSON wire envelope produced by
                :func:`encode_input_event`.

        Raises:
            BedrockStreamError: If the send fails on the live stream.
        """
        try:
            await stream.input_stream.send(_input_chunk(payload))
        except _SDK_TRANSPORT_ERRORS as exc:
            raise BedrockStreamError(_SEND_FAILED_DETAIL) from exc
