"""Abstract interface to one Nova Sonic bidirectional Bedrock_Stream.

``BedrockStreamPort`` is the port boundary (Req 17.6) between
orchestration and the Bedrock ``InvokeModelWithBidirectionalStream``
API. Orchestration code depends only on this abstract base class; every
wire-level concern — the HTTP/2 connection in us-east-1 (Req 2.1), the
Nova Sonic event-grammar envelopes, and base64 audio encoding — lives
exclusively in the implementing adapter
(``adapters.bedrock_stream_client``). Test suites substitute the
deterministic in-memory ``FakeBedrockStream``.

One port instance represents exactly one Bedrock_Stream lifecycle:

1. :meth:`BedrockStreamPort.open` establishes the stream and delivers
   the initial event sequence built by
   ``domain.segmentation.build_replay_sequence`` (``sessionStart`` →
   ``promptStart`` → system-prompt block → history blocks).
2. :meth:`BedrockStreamPort.send_event`,
   :meth:`BedrockStreamPort.send_audio`, and
   :meth:`BedrockStreamPort.send_tool_result` feed input events into the
   live stream.
3. :meth:`BedrockStreamPort.receive` yields the stream's decoded output
   events.
4. :meth:`BedrockStreamPort.close` tears the stream down gracefully
   (``promptEnd`` → ``sessionEnd``, Req 2.5).

A Voice_Session that outlives one stream — Session_Segmentation at
7 m 30 s (Req 2.2) or reconnect restoration (Req 8.3) — opens a fresh
port instance for each replacement Bedrock_Stream and replays context
through :meth:`BedrockStreamPort.open`.

Both directions speak plain JSON-ready mappings discriminated by a
``"type"`` key. Inputs use the ``ReplayEvent`` shape emitted by
``domain.segmentation``; outputs carry the Nova Sonic output vocabulary
(``completionStart``, ``contentStart``, ``textOutput``, ``audioOutput``,
``toolUse``, ``contentEnd``, ``completionEnd``).

Implementations raise only the ``BedrockStreamError`` branch of the
portal exception hierarchy: ``StreamOpenError`` when a stream cannot be
established (Req 1.6) and ``BedrockStreamError`` when an established
stream fails. This module imports no SDK (Req 17.6).
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence

from app.domain.segmentation import ReplayEvent

__all__ = ["BedrockStreamPort"]


class BedrockStreamPort(ABC):
    """One Nova Sonic bidirectional stream, as orchestration sees it.

    Abstract base class over a single Bedrock_Stream: open it once with
    the initial replay sequence, exchange input and output events while
    it is live, and close it exactly once when the segment ends. The
    voice session manager owns instance lifecycles — one instance per
    Bedrock_Stream, a fresh instance per segmentation rollover or
    reconnect replay (Req 2.2, 8.3).
    """

    @abstractmethod
    async def open(self, events: Sequence[ReplayEvent]) -> None:
        """Establish the stream and deliver the initial event sequence.

        Opens the bidirectional stream and sends every event of
        ``events`` in order before returning, so the conversation
        context is fully reconstructed before any audio flows
        (Req 2.3). Must be called exactly once per instance, before any
        other method.

        Args:
            events: Initial input events in the exact order to deliver,
                as built by ``domain.segmentation.build_replay_sequence``:
                ``sessionStart``, ``promptStart`` (carrying the
                ``toolConfiguration`` with the ``ask_devops_agent`` tool,
                Req 3.1), the system-prompt text block, and any replayed
                history blocks. The sequence contains no audio event; the
                caller flushes buffered audio afterward via
                :meth:`send_audio` (Req 2.4).

        Raises:
            StreamOpenError: If the stream cannot be established or the
                initial sequence cannot be delivered (Req 1.6).
        """

    @abstractmethod
    async def send_event(self, event: ReplayEvent) -> None:
        """Send one input event into the live stream.

        Generic escape hatch for any Nova Sonic input event the
        convenience methods do not cover — for example the
        ``contentStart`` opening the interactive audio content block,
        a ``textInput``, or a ``contentEnd``. The adapter serializes
        the mapping into the wire-level event grammar.

        Args:
            event: One JSON-ready input event discriminated by its
                ``"type"`` key, in the ``ReplayEvent`` shape emitted by
                ``domain.segmentation``.

        Raises:
            BedrockStreamError: If the stream is not open or the send
                fails on the live stream.
        """

    @abstractmethod
    async def send_audio(
        self, prompt_name: str, content_name: str, pcm: bytes
    ) -> None:
        """Send one ``audioInput`` event carrying raw engineer audio.

        Convenience for the inbound audio pump (Req 1.2): the adapter
        base64-encodes ``pcm`` into the ``audioInput`` event's content.
        The enclosing audio content block is opened and closed by the
        caller through :meth:`send_event` (``contentStart`` with AUDIO
        type, then repeated :meth:`send_audio`, then ``contentEnd``).

        Args:
            prompt_name: Prompt identifier of the live stream, as
                carried by its ``promptStart`` event.
            content_name: Name of the currently open audio content
                block the audio belongs to.
            pcm: Raw 16 kHz 16-bit mono PCM audio bytes exactly as
                received from the client (Req 1.1); encoding for the
                wire is the adapter's concern.

        Raises:
            BedrockStreamError: If the stream is not open or the send
                fails on the live stream.
        """

    @abstractmethod
    async def send_tool_result(
        self, prompt_name: str, tool_use_id: str, content: str
    ) -> None:
        """Send one complete tool-result content block.

        Emits the three-event tool-result block ``contentStart`` (TOOL,
        ``tool_use_id``) → ``toolResult`` → ``contentEnd``, returning
        the ``ask_devops_agent`` answer (or error indication) to
        Nova_Sonic so it speaks the outcome to the engineer
        (Req 3.4, 3.7).

        Args:
            prompt_name: Prompt identifier of the live stream, as
                carried by its ``promptStart`` event.
            tool_use_id: The ``toolUseId`` from the ``toolUse`` output
                event this result answers.
            content: The tool-result payload as stringified JSON —
                ``{"answer": "..."}`` on success or ``{"error": "..."}``
                on failure or refusal.

        Raises:
            BedrockStreamError: If the stream is not open or the send
                fails on the live stream.
        """

    @abstractmethod
    def receive(self) -> AsyncIterator[Mapping[str, object]]:
        """Return the stream's decoded output events as an async iterator.

        The outbound pump iterates this to relay ``audioOutput`` as
        binary frames (Req 1.3), ``textOutput`` as transcript frames
        (Req 1.4), and ``toolUse`` to the tool router. Iteration ends
        when the stream completes — after graceful teardown or when the
        service closes the response stream.

        Returns:
            An async iterator of decoded output events, each a
            JSON-ready mapping discriminated by its ``"type"`` key:
            ``completionStart``, ``contentStart``, ``textOutput``,
            ``audioOutput``, ``toolUse``, ``contentEnd``, or
            ``completionEnd``. Payload fields keep their wire values
            (``audioOutput`` content remains base64 text; the caller
            decodes it), so every yielded mapping stays JSON-ready.

        Raises:
            BedrockStreamError: Raised by the iterator if the live
                stream fails mid-iteration.
        """

    @abstractmethod
    async def close(self) -> None:
        """Tear the stream down gracefully.

        Performs the teardown handshake (``promptEnd`` → ``sessionEnd``)
        and closes the underlying connection (Req 2.5). Closing an
        already-closed (or never-opened) instance is a no-op, so
        orchestration can call it unconditionally on every exit path.

        Raises:
            BedrockStreamError: If the teardown of a live stream fails.
        """
