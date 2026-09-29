"""AWS DevOps Agent chat adapter over the blocking boto3 client.

Concrete ``DevOpsAgentPort`` implementation
(``ports.devops_agent.DevOpsAgentPort``) carrying the portal's two agent
operations over the boto3 ``devops-agent`` service client:
:meth:`DevOpsAgentClient.create_chat` stands for ``aidevops:CreateChat``
(Req 3.2) and :meth:`DevOpsAgentClient.send_message` for
``aidevops:SendMessage``, whose streamed response is exposed as an async
iterator of text chunks in arrival order (Req 3.3). Adapter modules are
the only place AWS SDKs may be imported (Req 17.6, enforced by the
import-linter contract in ``pyproject.toml``).

**SDK availability.** boto3 arrives transitively through the pinned
``aioboto3`` dependency. At the time of writing that pin resolves to a
botocore whose service-model catalog does **not** yet include
``devops-agent``, so instantiating the client raises botocore's
``UnknownServiceError``. Client creation is therefore deferred to the
first call and wrapped: the failure surfaces as ``AgentRequestError``
naming the operation being attempted, with the SDK error chained as the
cause for diagnostics (Req 3.7). Once a botocore shipping the model is
installed, the same code path starts succeeding without change; no
dependency pin is altered by this module.

**Async isolation (Req 17.2, 17.3).** boto3 is synchronous, so no SDK
call runs on the event loop. ``CreateChat`` (and one-time client
creation) run under ``asyncio.to_thread``. ``SendMessage`` returns an
event stream whose iteration blocks on socket reads, so a dedicated
worker thread (:func:`_pump_stream`) iterates it and hands each text
chunk to the event loop via ``loop.call_soon_threadsafe`` into an
``asyncio.Queue``; the stream terminates with a ``_StreamEnd`` marker
carrying the worker's failure, if any. The async generator returned by
:meth:`DevOpsAgentClient.send_message` only awaits that queue, so the
event loop never blocks on the SDK.

**Early termination.** When the consumer stops iterating before the
stream is drained — expiry of the tool router's 60-second budget
(Req 3.8) or any cancellation — the generator's teardown sets a
``threading.Event`` the worker checks between events and closes the
underlying stream (best effort) to unblock a pending socket read.
Teardown is deliberately synchronous: an async generator must not await
in ``finally`` once ``GeneratorExit`` is delivered. Limitation: a worker
blocked in a read that survives the stream close exits only when
botocore's read timeout fires; the worker is a daemon thread, so it can
never prevent process exit.

**Execution scoping (Req 3.5, 3.6).** ``executionId`` is sent on
``CreateChat`` exactly when the caller provides one. Because the port
signature keeps ``send_message(chat_id, text)`` free of scoping, the
adapter records each created chat's execution scope in a process-local
mapping and replays it as ``executionId`` on every ``SendMessage`` for
that chat. A chat id restored from the Session_Store into a fresh
process has no cached scope and is sent without ``executionId`` — the
chat itself remains scoped server-side from its creation.

**Lenient response parsing.** The service surface is young, so response
shapes are inspected defensively rather than assumed: the chat id is
looked up under ``chatId``, ``id``, then ``chat.id``; the event stream
under ``responseStream``, ``messageStream``, then ``stream``; chunk text
under ``chunk`` / ``delta`` / ``content`` / ``text`` (nested mappings
searched depth-first) plus botocore's conventional ``bytes`` payload
key, with UTF-8 bytes decoded. Events carrying no recognizable text are
skipped; a response with no recognizable chat id or stream raises
``AgentRequestError`` for the operation (Req 3.7).

**Encryption in transit (Req 12.7).** botocore's endpoint resolver
produces only ``https://`` endpoints for AWS services, so every agent
call travels over TLS; no plaintext fallback exists.
"""

import asyncio
import contextlib
import functools
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from typing import Any, Final, Literal, final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.exceptions import AgentRequestError
from app.ports.devops_agent import DevOpsAgentPort

__all__ = ["ClientFactory", "DevOpsAgentClient"]

_SERVICE_NAME: Final = "devops-agent"
"""boto3 service name of the AWS DevOps Agent."""

_CHAT_ID_KEYS: Final[tuple[str, ...]] = ("executionId", "chatId", "id")
"""Top-level ``CreateChat`` response keys tried, in order, for the chat id.

``executionId`` comes first because it is what the service model actually
returns: ``CreateChatResponse`` is ``{executionId, createdAt}`` and has no
``chatId`` member. The chat's execution id IS its handle — ``SendMessage``
takes it as the required ``executionId`` parameter. The legacy names are
retained as lenient fallbacks.
"""

_NESTED_CHAT_KEY: Final = "chat"
"""``CreateChat`` response key whose nested ``id`` is the fallback chat id."""

_STREAM_KEYS: Final[tuple[str, ...]] = (
    "events",
    "responseStream",
    "messageStream",
    "stream",
)
"""``SendMessage`` response keys tried, in order, for the event stream.

``events`` comes first: it is the event-stream payload member of
``SendMessageResponse`` in the service model. The others are lenient
fallbacks.
"""

_TEXT_KEYS: Final[tuple[str, ...]] = ("chunk", "delta", "content", "text", "bytes")
"""Event keys searched, in order, for streamed text content.

Fallback path only: the documented content-block shapes are matched
explicitly by :func:`_extract_text` first. Kept for forward compatibility
with event shapes the service may add.

``bytes`` comes last: it is botocore's conventional raw-payload key for
stream chunk events, consulted only when no explicit text key matches.
"""

_TEXT_SEARCH_DEPTH: Final = 6
"""Maximum nesting depth searched by the fallback text search.

Six, not four: the content-block model nests an answer fragment five
levels deep (``contentBlockDelta`` → ``delta`` → ``textDelta`` → ``text``
inside the event envelope), and a four-level bound silently skipped every
chunk — the agent appeared to answer with nothing at all.
"""

_CONTENT_BLOCK_DELTA_EVENT: Final = "contentBlockDelta"
"""Stream event carrying one incremental fragment of the answer."""

_DELTA_KEY: Final = "delta"
"""Member of ``contentBlockDelta`` holding the delta union."""

_TEXT_DELTA_KEY: Final = "textDelta"
"""Delta-union member carrying an answer text fragment."""

_SUMMARY_EVENT: Final = "summary"
"""Stream event summarizing the agent's actions (answer fallback)."""

_RESPONSE_FAILED_EVENT: Final = "responseFailed"
"""Stream event reporting that the agent's response failed."""

DEFAULT_MODEL_TIER: Final = "fast"
"""``SendMessage`` model tier requested by default.

The service model accepts ``smart``, ``balanced``, or ``fast``, and
defaults to ``balanced`` when the field is absent. ``fast`` is chosen here
because the engineer is waiting through a spoken conversation: the tool
result cannot be spoken until the agent's answer is complete, and the
diagnostic prompt asks the model to check several candidate causes, so
each additional second is paid once per call. Raise this to ``balanced``
or ``smart`` (constructor argument) if answers turn out too shallow for
the diagnosis being asked of the agent.
"""

type ClientFactory = Callable[[], Any]
"""Zero-argument factory producing a blocking ``devops-agent`` client.

The default factory builds the real boto3 client; tests inject factories
returning in-memory fakes so no AWS access occurs.
"""


@final
class _StreamEnd:
    """Terminal queue marker published by the worker thread.

    Attributes:
        error: The worker's failure when the stream ended abnormally;
            ``None`` after a normally drained (or stop-requested) stream.
    """

    __slots__ = ("error",)

    def __init__(self, error: Exception | None = None) -> None:
        """Initialize the marker with the worker's outcome.

        Args:
            error: The worker's failure when the stream ended abnormally;
                ``None`` after a normally drained stream.
        """
        self.error = error


def _default_client_factory(region: str) -> Any:
    """Create the real blocking boto3 ``devops-agent`` client.

    Runs inside ``asyncio.to_thread`` because client construction performs
    blocking service-model file I/O. Credentials come from boto3's default
    resolver chain (environment → profile → ECS container credentials →
    IMDS), which serves the Fargate task role in deployment.

    Args:
        region: AWS region hosting the DevOps Agent endpoints
            (``Settings.aws_region``).

    Returns:
        The blocking boto3 client for the ``devops-agent`` service.

    Raises:
        UnknownServiceError: (a ``BotoCoreError``) If the installed
            botocore does not ship the ``devops-agent`` service model —
            the case for the pinned SDK at the time of writing; callers
            convert this into ``AgentRequestError`` (Req 3.7).
    """
    return boto3.client(_SERVICE_NAME, region_name=region)


def _extract_chat_id(response: object) -> str | None:
    """Extract the created chat's identifier from a ``CreateChat`` response.

    Lenient by design (see module docstring): tries the top-level keys
    ``chatId`` then ``id``, then a nested ``chat.id``, accepting only
    non-empty strings.

    Args:
        response: Raw SDK response of a ``CreateChat`` call.

    Returns:
        The chat identifier, or ``None`` when the response carries no
        recognizable id (the caller raises ``AgentRequestError``).
    """
    if not isinstance(response, Mapping):
        return None
    for key in _CHAT_ID_KEYS:
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    nested = response.get(_NESTED_CHAT_KEY)
    if isinstance(nested, Mapping):
        value = nested.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _find_event_stream(response: object) -> Iterable[object] | None:
    """Locate the streamed-response iterable in a ``SendMessage`` response.

    Tries ``responseStream``, ``messageStream``, then ``stream``, accepting
    any iterable that is not itself a string, bytes, or mapping (botocore's
    ``EventStream`` qualifies).

    Args:
        response: Raw SDK response of a ``SendMessage`` call.

    Returns:
        The event iterable, or ``None`` when the response carries no
        recognizable stream (the worker raises ``AgentRequestError``).
    """
    if not isinstance(response, Mapping):
        return None
    for key in _STREAM_KEYS:
        stream = response.get(key)
        if isinstance(stream, Iterable) and not isinstance(
            stream, (str, bytes, Mapping)
        ):
            return stream
    return None


def _as_text(value: object) -> str | None:
    """Return a candidate value as text when it is a string or UTF-8 bytes.

    Args:
        value: Candidate chunk content taken from a stream event.

    Returns:
        The string itself, the decoded text for UTF-8 bytes, or ``None``
        for any other type or non-UTF-8 bytes (the event is skipped).
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return decoded
    return None


def _extract_text(event: object, depth: int = _TEXT_SEARCH_DEPTH) -> str | None:
    """Extract the answer text of one stream event, if it carries any.

    Matches the documented content-block shape first —
    ``{"contentBlockDelta": {"delta": {"textDelta": {"text": ...}}}}``,
    the only shape ``SendMessage`` emits for answer text — then falls back
    to the lenient :data:`_TEXT_KEYS` search for forward compatibility.
    ``jsonDelta`` fragments are deliberately ignored: they carry the
    agent's structured tool arguments, not prose for the engineer.

    Args:
        event: One member of the ``SendMessage`` event stream.
        depth: Remaining nesting depth for the fallback search, bounding
            the recursion on unexpectedly deep shapes.

    Returns:
        The chunk text (possibly empty), or ``None`` when the event
        carries none.
    """
    if not isinstance(event, Mapping):
        return None
    explicit = _content_block_text(event)
    if explicit is not None:
        return explicit
    return _lenient_text(event, depth)


def _content_block_text(event: Mapping[str, object]) -> str | None:
    """Read an answer fragment from a content-block delta event.

    Args:
        event: One decoded stream event.

    Returns:
        The text fragment when the event is a ``contentBlockDelta``
        carrying a ``textDelta``, else ``None``.
    """
    block = event.get(_CONTENT_BLOCK_DELTA_EVENT)
    if not isinstance(block, Mapping):
        return None
    delta = block.get(_DELTA_KEY)
    if not isinstance(delta, Mapping):
        return None
    text_delta = delta.get(_TEXT_DELTA_KEY)
    if not isinstance(text_delta, Mapping):
        return None
    return _as_text(text_delta.get("text"))


def _summary_text(event: Mapping[str, object]) -> str | None:
    """Read the agent's action summary from a ``summary`` event.

    Args:
        event: One decoded stream event.

    Returns:
        The summary content when the event is a ``summary`` carrying
        non-empty content, else ``None``.
    """
    summary = event.get(_SUMMARY_EVENT)
    if not isinstance(summary, Mapping):
        return None
    text = _as_text(summary.get("content"))
    return text or None


def _response_failure(event: Mapping[str, object]) -> str | None:
    """Read the failure description from a ``responseFailed`` event.

    Args:
        event: One decoded stream event.

    Returns:
        ``"<errorCode>: <errorMessage>"`` (whichever parts are present)
        when the event reports a failed response, else ``None``.

    """
    failed = event.get(_RESPONSE_FAILED_EVENT)
    if not isinstance(failed, Mapping):
        return None
    parts = [
        str(failed[key])
        for key in ("errorCode", "errorMessage")
        if isinstance(failed.get(key), str) and failed[key]
    ]
    return ": ".join(parts) if parts else "response failed"


def _client_error_code(error: BaseException) -> str | None:
    """Read the API error code from a botocore ``ClientError``.

    Recorded as the ``AgentRequestError`` detail so orchestration can tell
    a throttled agent ("busy, back off") from a broken one, and so the
    operational log names the actual service error instead of a generic
    failure.

    Args:
        error: The exception raised by the SDK call.

    Returns:
        The service's ``Error.Code`` when the exception carries a botocore
        response, else ``None``.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    error_body = response.get("Error")
    if not isinstance(error_body, Mapping):
        return None
    code = error_body.get("Code")
    return code if isinstance(code, str) and code else None


def _raise_if_response_failed(event: Mapping[str, object]) -> None:
    """Convert a ``responseFailed`` event into the port's failure error.

    Without this the event was ignored and the answer simply accumulated
    to nothing, so an agent-side failure reached the engineer as silence.

    Args:
        event: One decoded stream event.

    Raises:
        AgentRequestError: With operation ``"SendMessage"`` and the
            service-reported detail when the event reports a failed
            response (Req 3.7).
    """
    failure = _response_failure(event)
    if failure is not None:
        raise AgentRequestError("SendMessage", failure)


def _lenient_text(event: object, depth: int) -> str | None:
    """Search an event for text under the legacy/forward-compatible keys.

    Depth-first: for each key in :data:`_TEXT_KEYS` (in order), a string
    or UTF-8-bytes value is the chunk text, and a nested mapping is
    searched recursively.

    Args:
        event: One member of the ``SendMessage`` event stream, or a nested
            value of one.
        depth: Remaining nesting depth to search.

    Returns:
        The chunk text (possibly empty), or ``None`` when the event
        carries none.
    """
    if depth <= 0 or not isinstance(event, Mapping):
        return None
    for key in _TEXT_KEYS:
        if key not in event:
            continue
        value = event[key]
        text = _as_text(value)
        if text is not None:
            return text
        nested = _lenient_text(value, depth - 1)
        if nested is not None:
            return nested
    return None


def _close_stream_quietly(stream: object) -> None:
    """Close an event stream, suppressing teardown errors.

    Called from the consumer's synchronous teardown to unblock a worker
    thread pending on a socket read. A missing ``close`` attribute (an
    already-exhausted or foreign iterable) is a no-op; close failures are
    suppressed because the stream is already being abandoned and a
    teardown error would mask the consumer's real exit reason.

    Args:
        stream: The event stream (or any iterable) to close.
    """
    close = getattr(stream, "close", None)
    if callable(close):
        with contextlib.suppress(BotoCoreError, ClientError, OSError):
            close()


def _publish(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[str | _StreamEnd],
    item: str | _StreamEnd,
) -> None:
    """Hand one item from the worker thread to the consumer's queue.

    Uses ``loop.call_soon_threadsafe`` — the only thread-safe way to touch
    an ``asyncio.Queue`` — so the consumer wakes without the event loop
    ever blocking (Req 17.3). When the loop is already closed the consumer
    is gone and the item is dropped.

    Args:
        loop: Event loop the consumer runs on.
        queue: Chunk queue consumed by ``send_message``.
        item: Text chunk or terminal ``_StreamEnd`` marker.
    """
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(queue.put_nowait, item)


def _open_stream(client: Any, request: Mapping[str, str]) -> Iterable[object]:
    """Call ``SendMessage`` and locate the event stream in its response.

    Runs in the worker thread: both the call itself and the returned
    stream's later iteration block on sockets.

    Args:
        client: Blocking ``devops-agent`` client.
        request: Keyword arguments of the ``SendMessage`` call.

    Returns:
        The response's event iterable.

    Raises:
        AgentRequestError: If the response carries no recognizable stream
            (Req 3.7). SDK failures propagate to the worker's boundary
            handler in :func:`_pump_stream`.
    """
    response = client.send_message(**request)
    stream = _find_event_stream(response)
    if stream is None:
        raise AgentRequestError("SendMessage")
    return stream


def _pump_stream(
    client: Any,
    request: Mapping[str, str],
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[str | _StreamEnd],
    stop: threading.Event,
    stream_holder: list[Iterable[object]],
) -> None:
    """Bridge the blocking event stream to the consumer's queue (worker body).

    Calls ``SendMessage``, publishes the stream reference for the
    consumer's teardown, then iterates events — checking ``stop`` between
    events so consumer cancellation is honored promptly — and publishes
    each recognizable text chunk in arrival order (Req 3.3). Always
    terminates the queue with a ``_StreamEnd``: carrying the failure on
    any error, empty after a normal drain or a stop request.

    Args:
        client: Blocking ``devops-agent`` client.
        request: Keyword arguments of the ``SendMessage`` call.
        loop: Event loop the consumer runs on.
        queue: Chunk queue consumed by ``send_message``.
        stop: Set by the consumer's teardown to request prompt exit.
        stream_holder: Receives the opened stream so the consumer can
            close it during teardown.
    """
    try:
        stream = _open_stream(client, request)
        stream_holder.append(stream)
        # Action summaries are held back and published only when the
        # answer carried no prose at all, so a response made entirely of
        # tool activity still reaches the engineer instead of arriving
        # empty — without duplicating a real answer.
        summaries: list[str] = []
        answered = False
        for event in stream:
            if stop.is_set():
                break
            if isinstance(event, Mapping):
                _raise_if_response_failed(event)
                summary = _summary_text(event)
                if summary is not None:
                    summaries.append(summary)
                    continue
            text = _extract_text(event)
            if text is not None:
                answered = True
                _publish(loop, queue, text)
        if not answered and summaries and not stop.is_set():
            _publish(loop, queue, " ".join(summaries))
    # Thread boundary: an exception escaping a worker thread would vanish
    # into the thread excepthook and leave the consumer waiting forever,
    # so every failure — SDK or parsing — is marshalled across the thread
    # boundary and re-raised on the event loop as AgentRequestError.
    except Exception as exc:  # noqa: BLE001
        _publish(loop, queue, _StreamEnd(exc))
    else:
        _publish(loop, queue, _StreamEnd())


def _raise_send_failure(end: _StreamEnd) -> None:
    """Re-raise a worker failure as the port's ``SendMessage`` error.

    Args:
        end: Terminal marker received from the worker thread.

    Raises:
        AgentRequestError: When ``end`` carries a failure — the worker's
            own ``AgentRequestError`` unchanged (unrecognizable response
            shape), any other exception wrapped with operation
            ``"SendMessage"`` and chained as the cause (Req 3.7).
    """
    if end.error is None:
        return
    if isinstance(end.error, AgentRequestError):
        raise end.error
    raise AgentRequestError(
        "SendMessage", _client_error_code(end.error)
    ) from end.error


@final
class DevOpsAgentClient(DevOpsAgentPort):
    """``DevOpsAgentPort`` adapter over the blocking boto3 client.

    One instance serves the whole Voice_Service process and is safe for
    concurrent use from multiple sessions: the boto3 client is
    thread-safe, per-call state is local to each call, and the one-time
    client construction is guarded by an ``asyncio.Lock``. Chat lifecycle
    (one chat per Voice_Session, persistence, reuse) belongs to the tool
    router and the Session_Store, not to this adapter (Req 3.2, 3.9,
    3.10); the adapter only keeps the process-local chat-to-execution
    scope mapping described in the module docstring (Req 3.5, 3.6).
    """

    def __init__(
        self,
        agent_space_id: str,
        region: str,
        client_factory: ClientFactory | None = None,
        model_tier: str = DEFAULT_MODEL_TIER,
    ) -> None:
        """Initialize the adapter; no client is created until first use.

        Args:
            agent_space_id: DevOps Agent agent-space identifier sent as
                ``agentSpaceId`` on every call
                (``Settings.devops_agent_space_id``).
            region: AWS region used by the default client factory
                (``Settings.aws_region``).
            client_factory: Replacement zero-argument client factory;
                ``None`` selects the real boto3 factory. Tests inject
                factories returning in-memory fakes so no AWS access
                occurs.
            model_tier: ``SendMessage`` model tier — ``smart``,
                ``balanced``, or ``fast``; see
                :data:`DEFAULT_MODEL_TIER` for the latency tradeoff.
        """
        self._model_tier = model_tier
        self._agent_space_id = agent_space_id
        if client_factory is None:
            client_factory = functools.partial(_default_client_factory, region)
        self._client_factory: ClientFactory = client_factory
        self._client: Any = None
        self._client_lock = asyncio.Lock()
        self._chat_executions: dict[str, str | None] = {}

    async def create_chat(self, execution_id: str | None) -> str:
        """Create a DevOps_Agent chat, optionally execution-scoped.

        The blocking ``CreateChat`` call runs in a worker thread via
        ``asyncio.to_thread`` (Req 17.3). ``executionId`` is included in
        the request exactly when ``execution_id`` is provided (Req 3.5,
        3.6), and the resulting scope is cached per chat id for later
        ``SendMessage`` calls (see module docstring).

        Args:
            execution_id: DevOps_Agent execution to scope the chat to, or
                ``None`` for an unscoped chat.

        Returns:
            The created chat's identifier, parsed leniently from the
            response, for the caller to persist in the Session_Store
            (Req 3.2).

        Raises:
            AgentRequestError: With operation ``"CreateChat"`` when the
                client cannot be created (service model absent from the
                pinned SDK), the call fails, or the response carries no
                recognizable chat id (Req 3.7); the underlying error is
                chained as the cause.
        """
        client = await self._ensure_client("CreateChat")
        # ``CreateChatRequest`` accepts only agentSpaceId (URI), plus the
        # deprecated userId and the optional userType — there is NO
        # executionId member, so sending one fails botocore's parameter
        # validation before any request leaves the process. The incident
        # execution scope is therefore kept process-local (cached below)
        # and replayed on SendMessage's context instead.
        request: dict[str, str] = {"agentSpaceId": self._agent_space_id}
        try:
            response = await asyncio.to_thread(client.create_chat, **request)
        except (BotoCoreError, ClientError) as exc:
            raise AgentRequestError("CreateChat", _client_error_code(exc)) from exc
        chat_id = _extract_chat_id(response)
        if chat_id is None:
            raise AgentRequestError("CreateChat")
        self._chat_executions[chat_id] = execution_id
        return chat_id

    async def send_message(self, chat_id: str, text: str) -> AsyncIterator[str]:
        """Send one engineer request and yield streamed answer chunks.

        The blocking ``SendMessage`` call and its event-stream iteration
        run in a dedicated worker thread; chunks cross into the event loop
        through an ``asyncio.Queue``, so awaiting this generator never
        blocks the loop (Req 17.3). Chunks are yielded in arrival order
        for the caller to accumulate into one complete response text
        (Req 3.3). The chat's cached execution scope, when present, is
        replayed as ``executionId`` (Req 3.5). Teardown on early exit —
        including expiry of the caller's 60-second budget (Req 3.8) —
        signals the worker between events and closes the stream.

        Args:
            chat_id: Chat to send the request on — the identifier
                persisted for the Voice_Session (Req 3.9).
            text: The engineer request text, sent as ``content``.

        Yields:
            Streamed response text chunks in arrival order; chunks may be
            empty strings and are concatenated verbatim by the caller.

        Raises:
            AgentRequestError: With operation ``"SendMessage"`` when the
                client cannot be created, the call or the stream fails, or
                the response carries no recognizable stream (Req 3.7);
                raised on the first iteration step or mid-stream, with the
                underlying error chained as the cause.
        """
        client = await self._ensure_client("SendMessage")
        request: dict[str, str] = {
            # ``SendMessageRequest`` requires agentSpaceId, executionId,
            # and content — there is no chatId member. The chat handle
            # returned by CreateChat IS the executionId, so it is sent as
            # such; sending chatId failed parameter validation.
            "agentSpaceId": self._agent_space_id,
            "executionId": chat_id,
            "content": text,
            # Latency matters here: the engineer hears nothing until the
            # whole answer is accumulated (see DEFAULT_MODEL_TIER).
            "modelTier": self._model_tier,
        }

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | _StreamEnd] = asyncio.Queue()
        stop = threading.Event()
        stream_holder: list[Iterable[object]] = []
        worker = threading.Thread(
            target=_pump_stream,
            args=(client, request, loop, queue, stop, stream_holder),
            name=f"devops-agent-send-{chat_id}",
            daemon=True,
        )
        worker.start()
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _StreamEnd):
                    _raise_send_failure(item)
                    return
                yield item
        finally:
            # Synchronous-only teardown: once GeneratorExit is delivered,
            # an async generator must not await. Signal the worker (it
            # checks between events) and close the stream to unblock a
            # pending socket read (best effort; see module docstring).
            stop.set()
            for stream in stream_holder:
                _close_stream_quietly(stream)

    async def _ensure_client(
        self, operation: Literal["CreateChat", "SendMessage"]
    ) -> Any:
        """Create the boto3 client on first use, guarded by a lock.

        Client construction is blocking (service-model file I/O), so it
        runs under ``asyncio.to_thread``; the lock keeps concurrent first
        calls from constructing twice. Deferring construction to call time
        is what turns a missing ``devops-agent`` service model in the
        pinned SDK into a per-call ``AgentRequestError`` instead of an
        import-time crash (see module docstring).

        Args:
            operation: The agent operation being attempted, naming the
                ``AgentRequestError`` on factory failure.

        Returns:
            The cached blocking ``devops-agent`` client.

        Raises:
            AgentRequestError: If the client factory fails — including
                botocore's ``UnknownServiceError`` when the pinned SDK
                lacks the service model — with the factory error chained
                as the cause (Req 3.7).
        """
        async with self._client_lock:
            if self._client is None:
                try:
                    self._client = await asyncio.to_thread(self._client_factory)
                except (BotoCoreError, ClientError) as exc:
                    raise AgentRequestError(operation) from exc
            return self._client
