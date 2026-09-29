"""FastAPI application factory and endpoint wiring for the Voice_Service.

The composition root of the design: :func:`create_app` validates startup
configuration (Req 14.4), wires the adapters into the orchestration
components, and exposes the service surface —

- ``/ws/voice``: the engineer audio WebSocket (Req 1.1). The Cognito JWT
  travels as the ``("bearer", <jwt>)`` subprotocol pair and is validated
  **before** the connection is accepted; a missing, invalid, or expired
  token closes with 4401 and no Voice_Session is created (Req 7.3, 7.5,
  12.6). Starlette translates a close issued before ``accept()`` into an
  HTTP 403 rejection of the handshake, so an unauthenticated peer never
  completes the upgrade. Accepted connections are adapted to the
  session manager's ``VoiceConnection`` protocol and served by
  ``VoiceSessionManager.run_session``.
- ``/healthz``: readiness combining the drain state and the task
  protection readiness flag — 200 only while the task accepts new
  sessions and protection is confirmed (Req 10.5, 10.7).
- ``POST``/``DELETE /api/push-subscriptions``: JWT-authenticated
  registration and removal of Web_Push_Subscriptions, persisted through
  ``SessionStorePort`` **before** the 204 confirmation is returned to
  the caller (Req 6.1, 8.7).

Startup and shutdown: ``load_settings(os.environ)`` runs inside
:func:`create_app` — a missing required key raises ``ConfigurationError``
at import of ``app.main:app``, so the process exits non-zero before the
server binds (Req 14.4). The lifespan resolves secrets through the SSM
fetcher (a failure aborts startup, Req 14.5), installs the SIGTERM
handler that starts the drain manager (Req 10.5), and on exit closes
every adapter this module constructed. The SIGTERM handler deliberately
replaces uvicorn's own: an immediate graceful shutdown would kill live
sessions, whereas the drain manager lets them continue for the drain
window and notifies stragglers (Req 10.8); once the drain watchdog has
fanned out, the handler raises SIGINT so uvicorn's remaining handler
performs its normal exit (ECS's SIGKILL at ``stopTimeout`` is the
backstop).

Environment notes: ``ECS_AGENT_URI`` is injected by the ECS agent itself
at task startup and exists only inside a running ECS task, so it is
*not* part of the validated configuration manifest — it is read directly
from the environment at wiring time. When absent (local development),
task protection is wired to the in-module no-op port and a warning is
logged; every other behavior is unchanged.

Error discipline (Req 17.5): the two ``except Exception`` blocks in this
module — the WebSocket connection boundary and the HTTP 500 handler —
are the *only* broad catches permitted in the Voice_Service; they convert
unhandled errors into an ``internal`` error frame plus a 1011 close, or
a structured 500 response. All I/O is asynchronous end to end
(Req 17.2, 17.3), and this module imports no AWS SDK — SDK reach happens
through the adapter modules it wires (Req 17.6).

Tests inject pre-built collaborators through :class:`AppOverrides`, so
the full HTTP/WebSocket surface can be exercised against the in-memory
fakes without AWS access.
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Final, Protocol

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from app.adapters.bedrock_stream_client import BedrockStreamClient
from app.adapters.devops_agent_client import DevOpsAgentClient
from app.adapters.dynamodb_store import DynamoDbSessionStore
from app.adapters.ecs_task_protection import EcsTaskProtectionAdapter
from app.adapters.guardrail_client import GuardrailClient
from app.adapters.ssm_secret_fetcher import fetch_ssm_parameter
from app.auth.jwt_validator import (
    BEARER_SUBPROTOCOL,
    CognitoJwtValidator,
    ValidatedToken,
    extract_bearer_token,
)
from app.config import SecretFetcher, Settings, load_settings, resolve_secrets
from app.exceptions import AuthenticationError, SessionStoreError
from app.logging import configure_logging
from app.orchestration.drain_manager import DrainManager
from app.orchestration.protection_manager import ProtectionManager
from app.orchestration.tool_router import ToolRouter
from app.orchestration.voice_session_manager import (
    CLOSE_CODE_ERROR,
    INTERNAL_ERROR_MESSAGE,
    ConnectionClosed,
    VoiceSessionManager,
)
from app.ports.bedrock_stream import BedrockStreamPort
from app.ports.devops_agent import DevOpsAgentPort
from app.ports.guardrail import GuardrailPort
from app.ports.session_store import SessionStorePort, WebPushSubscription
from app.ports.task_protection import TaskProtectionPort
from app.protocol.ws_messages import (
    ErrorCategory,
    ErrorFrame,
    serialize_server_frame,
)

__all__ = [
    "ECS_AGENT_URI_ENV",
    "SYSTEM_PROMPT",
    "WS_CLOSE_AUTH_FAILED",
    "WS_CLOSE_TRY_AGAIN_LATER",
    "AppOverrides",
    "TokenValidator",
    "app",
    "create_app",
]

ECS_AGENT_URI_ENV: Final = "ECS_AGENT_URI"
"""Environment variable carrying the task-local ECS agent base URI.

Injected by the ECS agent itself at task startup, so it exists only
inside a running ECS task and is deliberately outside the validated
configuration manifest of ``app.config`` (Req 14.4 covers deployment
configuration, not agent-injected runtime facts). Absent in local
development, where task protection is wired to a no-op port.
"""

WS_CLOSE_AUTH_FAILED: Final = 4401
"""Close code for a WebSocket handshake that failed authentication.

Sent before ``accept()`` per the design (validate before accept, close
4401, no Voice_Session — Req 7.3); Starlette translates the pre-accept
close into an HTTP 403 rejection of the upgrade, which satisfies
"reject the connection with an authentication error" while never
admitting unauthenticated frames (Req 12.6).
"""

WS_CLOSE_TRY_AGAIN_LATER: Final = 1013
"""Close code for upgrades arriving while the task is draining (Req 10.5).

1013 (Try Again Later) tells well-behaved clients to reconnect —
``/healthz`` is already 503, so the ALB routes the retry to a healthy
task. Issued before ``accept()``, so the handshake is rejected.
"""

SYSTEM_PROMPT: Final = (
    "You are the voice assistant of the Nova Sonic Support Portal. You"
    " investigate live AWS environments for DevOps engineers and deliver"
    " preliminary analysis yourself. You are heard, not read: answer in"
    " short, concrete, conversational sentences without markup or lists.\n"
    "\n"
    "Operating rules:\n"
    "1. You CAN inspect the engineer's real AWS environment, through the"
    " ask_devops_agent tool. Call it for every question about actual"
    " state — instances and their ids, IAM roles and instance profiles,"
    " security group and NACL rules, VPC endpoints and routing, SSM"
    " agent status, alarms, logs, metrics, deployments, configuration,"
    " and likely root causes. Reading and describing existing"
    " configuration is exactly your job.\n"
    "2. Never claim you cannot access, retrieve, or see AWS data, and"
    " never tell the engineer to go run commands or open the console to"
    " gather facts you can obtain by calling the tool. Call the tool"
    " first; only if it returns an error or an explicit refusal do you"
    " say so plainly and suggest a next step.\n"
    "3. Diagnose, do not delegate. For an open-ended problem ('why can"
    " this instance not start an SSM session?'), work the likely causes"
    " yourself: call the tool as many times as needed to check each"
    " one — attached IAM role and its policies, outbound rules, VPC"
    " endpoints or NAT path, agent status — then state what you found,"
    " what it means, and the most probable cause.\n"
    "4. The only thing you never do is CHANGE anything. You do not"
    " create, modify, delete, terminate, restart, or grant access to any"
    " resource or IAM entity, and you never help circumvent that. When"
    " asked to change something, say that plainly and offer the"
    " equivalent inspection instead. Restrictions apply to actions, not"
    " to information.\n"
    "5. Because a tool call takes a few seconds, say a brief"
    " acknowledgement first — for example 'Let me check that' — then"
    " call the tool, so the engineer knows you are working.\n"
    "6. Pace your tool calls: make ONE call and wait for its result"
    " before making another, and never repeat the same question while an"
    " answer is still pending. If a call times out or reports that the"
    " agent is rate limited, say so plainly and wait for the engineer to"
    " decide — retrying straight away only deepens the rate limit.\n"
    "7. Ground every factual claim in a tool result rather than"
    " guessing. Ask one short clarifying question only when the request"
    " is genuinely ambiguous; otherwise investigate directly."
)
"""System prompt establishing the diagnostic persona (Req 3.1, 4.3).

Handed to the session manager for every Bedrock_Stream replay; sessions
opened from an incident notification get the incident summary and
severity appended by the session manager (Req 5.8).

Deliberately leads with CAPABILITY, not restriction. An earlier version
opened with "You are strictly read-only", and the model generalized that
into an inability to see anything: it answered questions about instance
ids, IAM roles, and security groups by stating it "cannot directly access
or retrieve live instance data" and telling the engineer to look for
themselves — without ever calling the tool, which is exactly the
"acts like a plain LLM" failure the portal exists to avoid. Rules 1-3
therefore establish what the assistant can do and require tool use for
factual questions, and rule 4 scopes the restriction to state CHANGES
("restrictions apply to actions, not to information"). The guardrail and
the mutation backstop enforce that boundary independently, so the prompt
does not need to be timid to keep the portal safe.
"""

_HEALTH_READY_STATUS: Final = "ready"
_HEALTH_UNAVAILABLE_STATUS: Final = "unavailable"

_AUTH_REQUIRED_DETAIL: Final = "A bearer access token is required."
_AUTH_FAILED_DETAIL: Final = "The presented access token is not valid."
_INVALID_BODY_DETAIL: Final = (
    "The request body must carry a subscription object with an endpoint"
    " and p256dh/auth keys."
)
_ENDPOINT_REQUIRED_DETAIL: Final = (
    "An endpoint must be supplied as a query parameter or a JSON body."
)
_STORE_UNAVAILABLE_DETAIL: Final = (
    "The subscription store is unavailable; retry shortly."
)
_INTERNAL_ERROR_DETAIL: Final = "An internal error occurred."

_WWW_AUTHENTICATE_HEADERS: Final = {"WWW-Authenticate": "Bearer"}

_MODULE_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class TokenValidator(Protocol):
    """Structural seam for the Cognito access-token validator.

    Matched by :class:`~app.auth.jwt_validator.CognitoJwtValidator` in
    production; tests inject a scripted stand-in through
    :class:`AppOverrides` so the auth-dependent endpoints run without a
    JWKS endpoint.
    """

    async def validate(self, token: str) -> ValidatedToken:
        """Validate one presented access token, failing closed on doubt.

        Args:
            token: The compact-serialized JWT presented by the client.

        Returns:
            The validation result carrying the engineer identity.

        Raises:
            AuthenticationError: When the token must be rejected
                (Req 7.3, 12.6).
        """
        ...

    async def aclose(self) -> None:
        """Release any transport resources the validator owns."""
        ...


@dataclass(frozen=True, slots=True)
class AppOverrides:
    """Optional pre-built collaborators for :func:`create_app`.

    The dependency-injection seam for tests: any field left ``None`` is
    wired to its real adapter, so production passes no overrides at all
    while tests can exercise the complete HTTP/WebSocket surface against
    the in-memory fakes (Req 17.6). Overridden collaborators are *not*
    closed at shutdown — their lifecycle belongs to whoever built them,
    mirroring the injected-client convention of the adapters.

    Attributes:
        settings: Replacement for ``load_settings(os.environ)``, letting
            tests build an application without populating the process
            environment. When the provided settings already carry a
            resolved ``origin_verify_secret``, the lifespan skips secret
            resolution entirely; otherwise ``resolve_secrets`` still
            runs through ``secret_fetcher`` (Req 14.5).
        secret_fetcher: Replacement for the SSM Parameter Store fetcher
            used by ``resolve_secrets`` at startup.
        token_validator: Replacement for the Cognito JWT validator used
            by the WebSocket handshake and the subscription API.
        stream_factory: Replacement factory for per-stream
            ``BedrockStreamPort`` instances.
        guardrail: Replacement ``GuardrailPort`` for the tool router.
        agent: Replacement ``DevOpsAgentPort`` for the tool router.
        store: Replacement ``SessionStorePort`` for sessions,
            transcripts, chats, and push subscriptions.
        task_protection: Replacement ``TaskProtectionPort`` for the
            protection manager.
    """

    settings: Settings | None = None
    secret_fetcher: SecretFetcher | None = None
    token_validator: TokenValidator | None = None
    stream_factory: Callable[[], BedrockStreamPort] | None = None
    guardrail: GuardrailPort | None = None
    agent: DevOpsAgentPort | None = None
    store: SessionStorePort | None = None
    task_protection: TaskProtectionPort | None = None


class _NoopTaskProtection(TaskProtectionPort):
    """No-op ``TaskProtectionPort`` for environments without an ECS agent.

    Wired when ``ECS_AGENT_URI`` is absent — local development, where no
    scale-in exists to protect against. Both transitions succeed
    immediately, so the protection manager reports ready and sessions
    run undisturbed; deployed tasks always carry the agent-injected URI
    and never use this class.
    """

    async def acquire(self, expiry_minutes: int) -> None:
        """Accept the acquire without contacting any agent.

        Args:
            expiry_minutes: Ignored; there is no protection to expire.
        """

    async def release(self) -> None:
        """Accept the release without contacting any agent."""


class _PushSubscriptionKeys(BaseModel):
    """Encryption keys of one browser push subscription (Req 6.1).

    Attributes:
        p256dh: Client public key for payload encryption.
        auth: Client authentication secret.
    """

    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class _PushSubscriptionPayload(BaseModel):
    """One browser ``PushSubscription.toJSON()`` object (Req 6.1).

    Unknown keys the browser adds (``expirationTime`` and future fields)
    are ignored; only the canonical shape of the push-subscriptions
    table item is persisted.

    Attributes:
        endpoint: Push service endpoint URL; its SHA-256 hex digest is
            the table's sort key.
        keys: Encryption keys of the subscription.
    """

    endpoint: str = Field(min_length=1)
    keys: _PushSubscriptionKeys


class _PushSubscriptionBody(BaseModel):
    """``POST /api/push-subscriptions`` request body.

    Matches the frontend push manager, which sends
    ``{"subscription": <PushSubscription.toJSON()>}`` (Req 6.1).

    Attributes:
        subscription: The browser push subscription to persist.
    """

    subscription: _PushSubscriptionPayload


class _PushUnsubscribeBody(BaseModel):
    """``DELETE /api/push-subscriptions`` JSON body alternative.

    The endpoint may arrive as the ``endpoint`` query parameter or as
    this body; push endpoints are long URLs, so a body avoids URL-length
    concerns.

    Attributes:
        endpoint: Push service endpoint URL of the subscription to
            remove.
    """

    endpoint: str = Field(min_length=1)


@dataclass(slots=True)
class _Wiring:
    """The wired collaborators serving one application instance.

    Built synchronously by :func:`_build_wiring` (every adapter is lazy,
    so construction performs no I/O) and completed by the lifespan,
    which resolves secrets into ``settings`` and installs the SIGTERM
    handler. Route handlers close over this object.

    Attributes:
        settings: Validated configuration; secret fields populated once
            the lifespan ran ``resolve_secrets`` (Req 14.5).
        validator: Access-token validator for the WebSocket handshake
            and the subscription API.
        store: Session_Store port backing sessions, transcripts, chats,
            and push subscriptions.
        session_manager: Per-connection orchestrator for ``/ws/voice``.
        protection: Live-session registry driving task protection and
            the readiness flag (Req 10.7).
        drain: SIGTERM drain scheduler (Req 10.5, 10.8).
        secret_fetcher: Startup secret fetcher for ``resolve_secrets``.
        closers: ``aclose`` callables of the adapters this module
            constructed itself, awaited in order at shutdown; overridden
            collaborators are never enrolled.
        drain_task: The running drain sequence task, once SIGTERM fired;
            held so it is neither garbage-collected nor orphaned at
            shutdown.
        sigterm_installed: Whether the lifespan managed to install the
            SIGTERM handler (impossible on non-main-thread loops such as
            test clients).
    """

    settings: Settings
    validator: TokenValidator
    store: SessionStorePort
    session_manager: VoiceSessionManager
    protection: ProtectionManager
    drain: DrainManager
    secret_fetcher: SecretFetcher
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    drain_task: asyncio.Task[None] | None = None
    sigterm_installed: bool = False


class _WebSocketVoiceConnection:
    """Adapts an accepted FastAPI WebSocket to ``VoiceConnection``.

    The session manager drives the framework-free ``VoiceConnection``
    protocol (Req 17.6); this adapter owns the translation: Starlette's
    disconnect signals — the ``websocket.disconnect`` message, a
    ``WebSocketDisconnect`` raised on send, and the ``RuntimeError``
    Starlette raises for operations on an already-closed socket — all
    become :class:`ConnectionClosed`, and ``close`` is idempotent per
    the protocol contract.
    """

    __slots__ = ("_websocket",)

    # Transport faults one WebSocket operation can raise: Starlette's
    # disconnect exception, its RuntimeError on use-after-close, and
    # socket-level OSError (ConnectionError included). Never a bare
    # Exception (Req 17.5).
    _TRANSPORT_ERRORS: Final = (WebSocketDisconnect, RuntimeError, OSError)

    def __init__(self, websocket: WebSocket) -> None:
        """Wrap one accepted WebSocket.

        Args:
            websocket: The connection, already accepted with the bearer
                subprotocol.
        """
        self._websocket = websocket

    async def send_text(self, data: str) -> None:
        """Send one text frame to the client.

        Args:
            data: Serialized JSON control frame.

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        try:
            await self._websocket.send_text(data)
        except self._TRANSPORT_ERRORS as exc:
            raise ConnectionClosed("send-text") from exc

    async def send_bytes(self, data: bytes) -> None:
        """Send one binary frame (raw 24 kHz PCM audio) to the client.

        Args:
            data: Raw PCM audio bytes (Req 1.3).

        Raises:
            ConnectionClosed: If the connection is already closed.
        """
        try:
            await self._websocket.send_bytes(data)
        except self._TRANSPORT_ERRORS as exc:
            raise ConnectionClosed("send-bytes") from exc

    async def receive(self) -> str | bytes:
        """Await the next frame from the client.

        Returns:
            The frame payload: ``str`` for text control frames, ``bytes``
            for binary PCM audio (Req 1.1).

        Raises:
            ConnectionClosed: When the client disconnected, the
                connection was closed locally, or the ASGI message
                carries no payload.
        """
        try:
            message = await self._websocket.receive()
        except self._TRANSPORT_ERRORS as exc:
            raise ConnectionClosed("receive") from exc
        if message["type"] == "websocket.disconnect":
            code = message.get("code")
            raise ConnectionClosed(f"close-code-{code}")
        text = message.get("text")
        if isinstance(text, str):
            return text
        data = message.get("bytes")
        if isinstance(data, bytes):
            return data
        # Per the ASGI spec exactly one of text/bytes is set; a message
        # with neither means the transport is unusable.
        raise ConnectionClosed("payload-missing")

    async def close(self, code: int, reason: str) -> None:
        """Close the connection; closing an already-closed one is safe.

        Args:
            code: WebSocket close code.
            reason: Short close reason token.
        """
        try:
            await self._websocket.close(code=code, reason=reason)
        except self._TRANSPORT_ERRORS:
            _MODULE_LOGGER.debug(
                "WebSocket close after the connection was already closed",
                extra={"close_code": code},
            )


def _endpoint_hash(endpoint: str) -> str:
    """Hash a push endpoint URL into the subscriptions table sort key.

    Args:
        endpoint: Push service endpoint URL.

    Returns:
        The SHA-256 hex digest of the UTF-8 encoded endpoint, making
        registrations idempotent per endpoint (design data model).
    """
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _bearer_token_from_header(authorization: str | None) -> str | None:
    """Extract the bearer token from an ``Authorization`` header value.

    Args:
        authorization: Raw header value, or ``None`` when the request
            carried no ``Authorization`` header.

    Returns:
        The token following a (case-insensitive) ``Bearer`` scheme, or
        ``None`` when the header is absent, uses another scheme, or
        carries no token.
    """
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def _authenticate_request(
    validator: TokenValidator, authorization: str | None
) -> ValidatedToken:
    """Authenticate one ``/api`` request from its Authorization header.

    Cognito authentication is required regardless of the transport path
    the request took (Req 7.5); a request that cannot present a fully
    valid access token is answered 401 and touches no data (Req 7.3,
    12.6).

    Args:
        validator: The access-token validator.
        authorization: Raw ``Authorization`` header value, if any.

    Returns:
        The validated token; its ``sub`` is the engineer identity the
        subscription records are keyed by.

    Raises:
        HTTPException: 401 with a ``WWW-Authenticate: Bearer`` header
            when the header is absent or malformed, or the token fails
            validation.
    """
    token = _bearer_token_from_header(authorization)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail=_AUTH_REQUIRED_DETAIL,
            headers=dict(_WWW_AUTHENTICATE_HEADERS),
        )
    try:
        return await validator.validate(token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail=_AUTH_FAILED_DETAIL,
            headers=dict(_WWW_AUTHENTICATE_HEADERS),
        ) from exc


def _task_protection_port(
    overrides: AppOverrides, closers: list[Callable[[], Awaitable[None]]]
) -> TaskProtectionPort:
    """Select the task protection port for this environment.

    Args:
        overrides: Test overrides; an injected port wins outright.
        closers: Shutdown registry; the real adapter's ``aclose`` is
            enrolled when one is constructed here.

    Returns:
        The injected port, the real ECS agent adapter when
        ``ECS_AGENT_URI`` is present (deployment), or the no-op port
        when it is absent (local development, documented on
        :data:`ECS_AGENT_URI_ENV`).
    """
    if overrides.task_protection is not None:
        return overrides.task_protection
    ecs_agent_uri = os.environ.get(ECS_AGENT_URI_ENV, "").strip()
    if ecs_agent_uri:
        adapter = EcsTaskProtectionAdapter(ecs_agent_uri)
        closers.append(adapter.aclose)
        return adapter
    _MODULE_LOGGER.warning(
        "ECS_AGENT_URI is not set; task scale-in protection is disabled"
        " (expected only in local development)"
    )
    return _NoopTaskProtection()


def _build_wiring(settings: Settings, overrides: AppOverrides) -> _Wiring:
    """Construct the object graph of one application instance.

    Every real adapter is lazy — construction performs no I/O and needs
    no credentials — so wiring is synchronous and safe at import time.
    Adapters constructed here are enrolled for shutdown; injected
    overrides are not (their lifecycle belongs to the injector).

    Args:
        settings: Validated configuration from :func:`load_settings`.
        overrides: Optional pre-built collaborators for tests.

    Returns:
        The wired collaborators serving this application instance.
    """
    closers: list[Callable[[], Awaitable[None]]] = []

    validator: TokenValidator
    if overrides.token_validator is not None:
        validator = overrides.token_validator
    else:
        owned_validator = CognitoJwtValidator(
            settings.cognito_user_pool_id,
            settings.cognito_client_id,
            settings.aws_region,
        )
        closers.append(owned_validator.aclose)
        validator = owned_validator

    store: SessionStorePort
    if overrides.store is not None:
        store = overrides.store
    else:
        owned_store = DynamoDbSessionStore(
            settings.sessions_table_name,
            settings.chats_table_name,
            settings.subscriptions_table_name,
            settings.transcripts_table_name,
            settings.aws_region,
        )
        closers.append(owned_store.aclose)
        store = owned_store

    guardrail: GuardrailPort
    if overrides.guardrail is not None:
        guardrail = overrides.guardrail
    else:
        owned_guardrail = GuardrailClient(
            settings.guardrail_id,
            settings.guardrail_version,
            settings.aws_region,
        )
        closers.append(owned_guardrail.aclose)
        guardrail = owned_guardrail

    agent: DevOpsAgentPort
    if overrides.agent is not None:
        agent = overrides.agent
    else:
        # The DevOps Agent adapter owns no async transport of its own
        # (boto3 behind asyncio.to_thread), so it has nothing to close.
        agent = DevOpsAgentClient(
            settings.devops_agent_space_id, settings.aws_region
        )

    stream_factory: Callable[[], BedrockStreamPort]
    if overrides.stream_factory is not None:
        stream_factory = overrides.stream_factory
    else:
        # One fresh adapter per Bedrock_Stream: segmentation rollovers
        # and reconnects call the factory again (Req 2.2, 8.3).
        stream_factory = partial(
            BedrockStreamClient,
            settings.nova_sonic_model_id,
            settings.aws_region,
        )

    tool_router = ToolRouter(
        guardrail,
        agent,
        store,
        agent_timeout_seconds=settings.agent_response_budget_seconds,
        retention_days=settings.retention_days,
    )
    protection = ProtectionManager(_task_protection_port(overrides, closers))
    drain = DrainManager()
    session_manager = VoiceSessionManager(
        stream_factory=stream_factory,
        store=store,
        tool_router=tool_router,
        protection=protection,
        drain=drain,
        system_prompt=SYSTEM_PROMPT,
        rollover_seconds=settings.segmentation_rollover_seconds,
        watchdog_seconds=settings.segmentation_watchdog_seconds,
        idle_warning_seconds=settings.idle_warning_seconds,
        idle_grace_seconds=settings.idle_grace_seconds,
        retention_days=settings.retention_days,
    )

    secret_fetcher = overrides.secret_fetcher
    if secret_fetcher is None:
        secret_fetcher = partial(fetch_ssm_parameter, region=settings.aws_region)

    return _Wiring(
        settings=settings,
        validator=validator,
        store=store,
        session_manager=session_manager,
        protection=protection,
        drain=drain,
        secret_fetcher=secret_fetcher,
        closers=closers,
    )


async def _drain_to_exit(drain: DrainManager) -> None:
    """Run the SIGTERM drain sequence, then hand shutdown to the server.

    Starts the drain (new upgrades rejected, ``/healthz`` 503 — Req 10.5),
    waits for the drain watchdog to notify and close the sessions still
    active at expiry (Req 10.8), then raises SIGINT so uvicorn's own
    handler performs its normal graceful exit; ECS's SIGKILL at
    ``stopTimeout`` remains the backstop should that exit stall.

    Args:
        drain: The drain manager to start.
    """
    await drain.begin_drain()
    await drain.wait_for_watchdog()
    signal.raise_signal(signal.SIGINT)


def _schedule_drain(wiring: _Wiring) -> None:
    """SIGTERM callback: start the drain sequence exactly once.

    Runs on the event loop (registered via ``add_signal_handler``), so
    spawning the drain task here is safe; repeated signals are ignored
    while the sequence is running.

    Args:
        wiring: The application wiring carrying the drain manager and
            the task slot that keeps the sequence alive.
    """
    if wiring.drain_task is not None and not wiring.drain_task.done():
        return
    wiring.drain_task = asyncio.get_running_loop().create_task(
        _drain_to_exit(wiring.drain), name="sigterm-drain"
    )


def _install_sigterm_handler(wiring: _Wiring) -> None:
    """Install the SIGTERM drain handler on the running loop (Req 10.5).

    Deliberately replaces uvicorn's SIGTERM handler — uvicorn's immediate
    graceful shutdown would kill live sessions, while the drain sequence
    lets them continue for the drain window (module docstring). On loops
    where signal handlers cannot be installed — non-main threads, as
    under test clients, or platforms without ``add_signal_handler`` —
    the limitation is logged and startup continues.

    Args:
        wiring: The application wiring; ``sigterm_installed`` records
            whether removal is owed at shutdown.
    """
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, partial(_schedule_drain, wiring))
    except (NotImplementedError, RuntimeError, ValueError):
        _MODULE_LOGGER.warning(
            "SIGTERM drain handler could not be installed on this event"
            " loop; connection draining is unavailable"
        )
        return
    wiring.sigterm_installed = True


async def _shutdown(wiring: _Wiring) -> None:
    """Release everything the lifespan owns, in reverse dependency order.

    Removes the SIGTERM handler, cancels a still-pending drain sequence,
    and closes every adapter this module constructed. Injected overrides
    are untouched.

    Args:
        wiring: The application wiring to tear down.
    """
    if wiring.sigterm_installed:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
        wiring.sigterm_installed = False
    if wiring.drain_task is not None and not wiring.drain_task.done():
        wiring.drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wiring.drain_task
    for closer in wiring.closers:
        await closer()


async def _fail_websocket(connection: _WebSocketVoiceConnection) -> None:
    """Answer an unhandled WebSocket error with an error frame and close.

    Best effort by design: the boundary owes the client an ``internal``
    error frame and a 1011 close (Req 17.5), but the connection may
    already be gone — a vanished peer never masks the boundary handling.

    Args:
        connection: The adapted connection the failure occurred on.
    """
    frame = ErrorFrame(
        category=ErrorCategory.INTERNAL,
        message=INTERNAL_ERROR_MESSAGE,
        recoverable=False,
    )
    with contextlib.suppress(ConnectionClosed):
        await connection.send_text(serialize_server_frame(frame))
    await connection.close(CLOSE_CODE_ERROR, "internal-error")


def create_app(overrides: AppOverrides | None = None) -> FastAPI:
    """Build the Voice_Service FastAPI application.

    Configures logging, validates the configuration manifest — a missing
    required key raises ``ConfigurationError`` here, before any server
    binds (Req 14.4) — wires the object graph, and registers the service
    endpoints and lifespan. The interactive docs endpoints are disabled:
    the service exposes exactly its designed surface.

    Args:
        overrides: Optional pre-built collaborators for tests; ``None``
            (production) wires the real adapters.

    Returns:
        The configured application, ready for uvicorn.

    Raises:
        ConfigurationError: If a required configuration key is missing
            or invalid, naming the key (Req 14.4).
    """
    configure_logging()
    resolved_overrides = overrides if overrides is not None else AppOverrides()
    settings = (
        resolved_overrides.settings
        if resolved_overrides.settings is not None
        else load_settings(os.environ)
    )
    wiring = _build_wiring(settings, resolved_overrides)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Resolve secrets, arm SIGTERM draining, and clean up on exit.

        Args:
            _: The application instance (unused; the wiring is closed
                over).

        Yields:
            None once startup completed; the service accepts requests
            only after secrets resolved (Req 14.5).

        Raises:
            ConfigurationError: If a secret retrieval fails, naming the
                secret key only — startup aborts before any request is
                accepted (Req 14.5, 14.6).
        """
        if wiring.settings.origin_verify_secret is None:
            # Overrides may supply already-resolved settings; resolution
            # runs only while a secret is still outstanding (Req 14.5).
            wiring.settings = await resolve_secrets(
                wiring.settings, wiring.secret_fetcher
            )
        _install_sigterm_handler(wiring)
        try:
            yield
        finally:
            await _shutdown(wiring)

    application = FastAPI(
        title="Nova Sonic Support Portal Voice Service",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # The wired components on app.state: route handlers close over the
    # wiring, so this is for observers — tests and diagnostics reach the
    # live drain/protection/store objects without private imports.
    # ``wiring.settings`` is re-assigned by the lifespan once secrets
    # resolve, so settings are read through ``state.wiring``.
    application.state.wiring = wiring
    application.state.drain = wiring.drain
    application.state.protection = wiring.protection
    application.state.store = wiring.store
    application.state.session_manager = wiring.session_manager
    application.state.validator = wiring.validator

    @application.get("/healthz")
    async def healthz() -> JSONResponse:
        """Report task readiness for the ALB health check.

        Combines the drain state and the protection readiness flag: 200
        only while the task accepts new WebSocket connections and task
        protection is confirmed; 503 once draining began (Req 10.5) or
        a protection acquire exhausted its retries (Req 10.7), so the
        ALB stops routing new sessions here.

        Returns:
            200 with ``{"status": "ready"}`` when ready, else 503 with
            ``{"status": "unavailable"}``.
        """
        ready = wiring.drain.accepting_new and wiring.protection.is_ready
        return JSONResponse(
            {
                "status": (
                    _HEALTH_READY_STATUS if ready else _HEALTH_UNAVAILABLE_STATUS
                )
            },
            status_code=200 if ready else 503,
        )

    @application.post("/api/push-subscriptions", status_code=204)
    async def register_push_subscription(request: Request) -> Response:
        """Register one Web_Push_Subscription for the caller (Req 6.1).

        Authenticates the bearer token first (Req 7.3, 7.5), then
        validates the body shape, persists the record through the
        Session_Store, and confirms with 204 only after the write
        returned (Req 8.7). Re-registering the same endpoint is
        idempotent (upsert keyed by the endpoint hash).

        Args:
            request: The incoming request; body is
                ``{"subscription": {endpoint, keys: {p256dh, auth}}}``.

        Returns:
            204 with no body once the subscription is durable.

        Raises:
            HTTPException: 401 without a valid token, 422 for a body
                that does not carry a usable subscription, 503 when the
                Session_Store write failed (the caller may retry,
                Req 6.7).
        """
        token = await _authenticate_request(
            wiring.validator, request.headers.get("Authorization")
        )
        try:
            body = _PushSubscriptionBody.model_validate_json(await request.body())
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=_INVALID_BODY_DETAIL
            ) from exc
        subscription = body.subscription
        record = WebPushSubscription(
            engineer_id=token.sub,
            endpoint_hash=_endpoint_hash(subscription.endpoint),
            subscription={
                "endpoint": subscription.endpoint,
                "keys": {
                    "p256dh": subscription.keys.p256dh,
                    "auth": subscription.keys.auth,
                },
            },
        )
        try:
            await wiring.store.put_subscription(record)
        except SessionStoreError as exc:
            _MODULE_LOGGER.exception(
                "Web push subscription could not be persisted"
            )
            raise HTTPException(
                status_code=503, detail=_STORE_UNAVAILABLE_DETAIL
            ) from exc
        # 204 strictly after the persist returned (Req 6.1, 8.7).
        return Response(status_code=204)

    @application.delete("/api/push-subscriptions", status_code=204)
    async def remove_push_subscription(request: Request) -> Response:
        """Remove one Web_Push_Subscription of the caller (Req 8.7).

        Authenticates the bearer token first, resolves the endpoint from
        the ``endpoint`` query parameter or a ``{"endpoint": ...}`` JSON
        body, deletes the record, and confirms with 204 only after the
        delete returned (Req 8.7). Deleting an absent record is a no-op,
        so removal is idempotent.

        Args:
            request: The incoming request carrying the endpoint to
                remove.

        Returns:
            204 with no body once the removal is durable.

        Raises:
            HTTPException: 401 without a valid token, 422 when no usable
                endpoint was supplied, 503 when the Session_Store delete
                failed.
        """
        token = await _authenticate_request(
            wiring.validator, request.headers.get("Authorization")
        )
        endpoint = request.query_params.get("endpoint")
        if not endpoint:
            raw_body = await request.body()
            if raw_body:
                try:
                    endpoint = _PushUnsubscribeBody.model_validate_json(
                        raw_body
                    ).endpoint
                except ValidationError as exc:
                    raise HTTPException(
                        status_code=422, detail=_ENDPOINT_REQUIRED_DETAIL
                    ) from exc
        if not endpoint:
            raise HTTPException(
                status_code=422, detail=_ENDPOINT_REQUIRED_DETAIL
            )
        try:
            await wiring.store.delete_subscription(
                token.sub, _endpoint_hash(endpoint)
            )
        except SessionStoreError as exc:
            _MODULE_LOGGER.exception(
                "Web push subscription could not be removed"
            )
            raise HTTPException(
                status_code=503, detail=_STORE_UNAVAILABLE_DETAIL
            ) from exc
        # 204 strictly after the delete returned (Req 8.7).
        return Response(status_code=204)

    @application.websocket("/ws/voice")
    async def voice_websocket(websocket: WebSocket) -> None:
        """Serve one engineer voice WebSocket connection (Req 1.1).

        Order of gates, all before ``accept()``: the drain gate rejects
        upgrades on a draining task (Req 10.5); then the JWT from the
        ``bearer`` subprotocol pair is validated — signature, expiry,
        issuer, client — and any failure closes with 4401 without
        creating a Voice_Session (Req 7.2, 7.3, 7.5, 12.5, 12.6). Only a
        fully valid token reaches ``accept()``, after which the session
        manager owns the connection. The trailing broad catch is this
        module's WebSocket boundary handler (Req 17.5): it converts an
        unhandled error into an ``internal`` error frame and a 1011
        close.

        Args:
            websocket: The upgrade request, not yet accepted.
        """
        if not wiring.drain.accepting_new:
            await websocket.close(
                code=WS_CLOSE_TRY_AGAIN_LATER, reason="draining"
            )
            return
        offered = websocket.scope.get("subprotocols") or []
        token = extract_bearer_token(offered)
        if token is None:
            await websocket.close(
                code=WS_CLOSE_AUTH_FAILED, reason="authentication required"
            )
            return
        try:
            validated = await wiring.validator.validate(token)
        except AuthenticationError as exc:
            _MODULE_LOGGER.warning(
                "WebSocket handshake rejected: %s", exc,
            )
            await websocket.close(
                code=WS_CLOSE_AUTH_FAILED, reason="authentication failed"
            )
            return
        await websocket.accept(subprotocol=BEARER_SUBPROTOCOL)
        connection = _WebSocketVoiceConnection(websocket)
        try:
            await wiring.session_manager.run_session(connection, validated)
        except Exception:
            # Top-level WebSocket boundary (Req 17.5): the only place a
            # broad catch is permitted on this path.
            _MODULE_LOGGER.exception(
                "Unhandled error at the voice WebSocket boundary"
            )
            await _fail_websocket(connection)

    async def unhandled_http_error(request: Request, exc: Exception) -> JSONResponse:
        """Convert an unhandled route error into a structured 500.

        The HTTP counterpart of the WebSocket boundary handler
        (Req 17.5): Starlette's server-error middleware routes every
        exception no route handled here, so callers receive a structured
        JSON body instead of a stack trace.

        Args:
            request: The request whose handling failed.
            exc: The unhandled exception.

        Returns:
            A 500 response with a generic detail message (no internals
            leak, Req 14.6).
        """
        _MODULE_LOGGER.error(
            "Unhandled error at the HTTP boundary: %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return JSONResponse(
            {"detail": _INTERNAL_ERROR_DETAIL}, status_code=500
        )

    application.add_exception_handler(Exception, unhandled_http_error)

    return application


app = create_app()
"""The uvicorn entrypoint (``uvicorn app.main:app``).

Built at import time on purpose: configuration validation runs before
the server can bind, so a misconfigured task exits non-zero without
accepting a single request (Req 14.4).
"""
