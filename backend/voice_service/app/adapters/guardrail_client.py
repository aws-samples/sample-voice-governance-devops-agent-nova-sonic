"""Bedrock Guardrail adapter over the bedrock-runtime ``ApplyGuardrail`` API (Req 4.1).

:class:`GuardrailClient` implements ``GuardrailPort`` by calling the
bedrock-runtime ``ApplyGuardrail`` operation with the configured guardrail
id and version (``Settings.guardrail_id`` / ``Settings.guardrail_version``)
and ``source="INPUT"``, so every engineer request is evaluated against the
guardrail carrying the Automated Reasoning read-only-operations policy
before any part of it may reach the DevOps_Agent (Req 4.1, 4.2).

Failure normalization (Req 4.6): every way an evaluation can fail is
surfaced as ``GuardrailUnavailableError`` with a stable, content-free
reason — ``throttled`` for SDK throttling error codes, ``timeout`` when
the evaluation exceeds its time budget, ``sdk error: <code>`` for any
other AWS error code, and ``connection error`` for client-side transport
faults. Reasons carry error codes only, never request text or response
payload contents, so the ``unavailable:<reason>`` audit tokens built by
``domain.guardrail_policy.decide`` stay safe to log. Successful responses
are handed to the lenient ``GuardrailEvaluation.from_response`` parser,
which never raises: malformed shapes degrade to markers that ``decide``
blocks as ``malformed`` (Req 4.7).

Encryption in transit (Req 12.7): the SDK's default endpoint resolution
addresses the bedrock-runtime service over HTTPS, so every evaluation
travels a TLS connection; nothing in this module downgrades the scheme or
disables certificate verification. A TLS connection that cannot be
established fails the SDK call, which this adapter maps to a
``connection error`` unavailability — the request is blocked rather than
retried over an unencrypted channel (Req 12.8).

Client lifecycle: the aioboto3 session and client are created lazily on
first use by an async ensure helper, not in ``__init__``. That keeps
construction synchronous — the application wiring builds the adapter
before the event loop exists — while one client instance is reused across
evaluations so pooled TLS connections and resolved credentials amortize
over the process lifetime. :meth:`GuardrailClient.aclose` is the matching
teardown for application shutdown; a closed adapter transparently
recreates its client on the next evaluation.

This module lives in ``app/adapters`` — the only package permitted to
import AWS SDKs (``aioboto3`` / ``botocore``), enforced by the
import-linter contract (Req 17.6).
"""

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any, Final

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from app.domain.guardrail_policy import GuardrailEvaluation
from app.exceptions import GuardrailUnavailableError
from app.ports.guardrail import GuardrailPort

__all__ = ["GuardrailClient"]

_BEDROCK_RUNTIME_SERVICE: Final = "bedrock-runtime"
"""AWS service name carrying the ``ApplyGuardrail`` operation."""

_SOURCE_INPUT: Final = "INPUT"
"""``ApplyGuardrail`` source: engineer requests are evaluated as model input."""

_DEFAULT_TIMEOUT_SECONDS: Final = 5.0
"""Default evaluation time budget; expiry blocks the request (Req 4.6)."""

_THROTTLING_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"ThrottlingException", "TooManyRequestsException"}
)
"""AWS error codes normalized to the ``throttled`` unavailability reason."""

_REASON_THROTTLED: Final = "throttled"
"""Unavailability reason for SDK throttling responses (Req 4.6)."""

_REASON_TIMEOUT: Final = "timeout"
"""Unavailability reason when an evaluation exceeds its budget (Req 4.6)."""

_REASON_CONNECTION_ERROR: Final = "connection error"
"""Unavailability reason for client-side transport faults (Req 4.6, 12.8)."""

_UNKNOWN_ERROR_CODE: Final = "unknown"
"""Placeholder code when a ``ClientError`` carries no usable error code."""


def _client_error_reason(error: ClientError) -> str:
    """Normalize one AWS service error into a content-free reason token.

    Reads only the error *code* of the failed call — never the error
    message or any response payload content — so the resulting reason is
    safe for audit logs (Req 4.5).

    Args:
        error: The ``ClientError`` raised by the SDK for a failed
            ``ApplyGuardrail`` call.

    Returns:
        ``throttled`` when the code is a recognized throttling code, else
        ``sdk error: <code>`` with ``unknown`` standing in for a missing
        or malformed code.
    """
    details = error.response.get("Error", {})
    code = details.get("Code") if isinstance(details, Mapping) else None
    if isinstance(code, str) and code in _THROTTLING_ERROR_CODES:
        return _REASON_THROTTLED
    label = code if isinstance(code, str) and code else _UNKNOWN_ERROR_CODE
    return f"sdk error: {label}"


class GuardrailClient(GuardrailPort):
    """``GuardrailPort`` adapter over bedrock-runtime ``ApplyGuardrail``.

    Evaluates engineer request text against the configured guardrail
    (Automated Reasoning read-only-operations policy) with
    ``source="INPUT"`` (Req 4.1). Any failure to complete an evaluation —
    throttle, timeout, service error, or transport fault — raises
    ``GuardrailUnavailableError`` so the fail-closed decision function
    blocks the request (Req 4.6); a successful response is returned as
    the leniently parsed ``GuardrailEvaluation``.

    One aioboto3 session/client pair is created lazily on the first
    evaluation and reused afterwards (see the module docstring for the
    lifecycle rationale); call :meth:`aclose` at application shutdown to
    release it. All traffic to the service uses the SDK's default HTTPS
    endpoints, so evaluations are TLS-protected in transit (Req 12.7).
    """

    def __init__(
        self,
        guardrail_id: str,
        guardrail_version: str,
        region: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the adapter without creating any AWS client yet.

        Args:
            guardrail_id: Identifier of the guardrail to apply, from
                ``Settings.guardrail_id`` (Req 4.1).
            guardrail_version: Version of the guardrail to apply, from
                ``Settings.guardrail_version``.
            region: AWS region hosting the bedrock-runtime endpoint,
                from ``Settings.aws_region``.
            timeout_seconds: Time budget for one evaluation, covering
                lazy client creation and the ``ApplyGuardrail`` call;
                expiry raises ``GuardrailUnavailableError`` with reason
                ``timeout`` (Req 4.6).
        """
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        self._region = region
        self._timeout_seconds = timeout_seconds
        self._client: Any = None
        self._exit_stack: AsyncExitStack | None = None
        self._client_lock = asyncio.Lock()

    async def evaluate(self, text: str) -> GuardrailEvaluation:
        """Evaluate one engineer request against the configured guardrail.

        Calls ``ApplyGuardrail`` with the configured guardrail id and
        version and ``source="INPUT"`` (Req 4.1), bounded by the
        adapter's time budget. Client creation happens lazily inside the
        same budget, so a hung first connection also surfaces as a
        ``timeout`` unavailability rather than stalling the gate.

        Args:
            text: The engineer request text extracted from the
                ``ask_devops_agent`` tool input, evaluated before any
                part of it may reach the DevOps_Agent (Req 4.1, 4.2).

        Returns:
            The response parsed by the lenient
            ``GuardrailEvaluation.from_response`` — malformed shapes
            degrade to markers that ``decide`` blocks as ``malformed``
            (Req 4.7) rather than raising here.

        Raises:
            GuardrailUnavailableError: If the evaluation cannot be
                completed (Req 4.6), with a content-free reason:
                ``throttled`` for throttling error codes, ``timeout``
                for budget expiry, ``sdk error: <code>`` for any other
                service error, or ``connection error`` for client-side
                transport faults — including a TLS connection that
                cannot be established (Req 12.8).
        """
        try:
            async with asyncio.timeout(self._timeout_seconds):
                client = await self._ensure_client()
                response = await client.apply_guardrail(
                    guardrailIdentifier=self._guardrail_id,
                    guardrailVersion=self._guardrail_version,
                    source=_SOURCE_INPUT,
                    content=[{"text": {"text": text}}],
                )
        except TimeoutError as exc:
            raise GuardrailUnavailableError(_REASON_TIMEOUT) from exc
        except ClientError as exc:
            raise GuardrailUnavailableError(_client_error_reason(exc)) from exc
        except BotoCoreError as exc:
            raise GuardrailUnavailableError(_REASON_CONNECTION_ERROR) from exc
        return GuardrailEvaluation.from_response(dict(response))

    async def aclose(self) -> None:
        """Release the lazily created AWS client and its connections.

        The teardown half of the lazy lifecycle: call at application
        shutdown. Closing an adapter that never evaluated is a no-op,
        and a closed adapter recreates its client on the next
        evaluation.
        """
        async with self._client_lock:
            stack = self._exit_stack
            self._client = None
            self._exit_stack = None
            if stack is not None:
                await stack.aclose()

    async def _ensure_client(self) -> Any:
        """Return the shared bedrock-runtime client, creating it on first use.

        The lock serializes concurrent first evaluations (and
        :meth:`aclose`), so exactly one session/client pair exists at a
        time. Creation failures propagate to :meth:`evaluate`, whose
        error mapping converts them into ``GuardrailUnavailableError``.

        Returns:
            The live aioboto3 bedrock-runtime client, connected to the
            configured region over the SDK's default HTTPS endpoint
            (Req 12.7).
        """
        async with self._client_lock:
            if self._client is None:
                stack = AsyncExitStack()
                try:
                    session = aioboto3.Session()
                    self._client = await stack.enter_async_context(
                        session.client(
                            _BEDROCK_RUNTIME_SERVICE, region_name=self._region
                        )
                    )
                except BaseException:
                    # Creation failed partway: unwind whatever the stack
                    # entered, then let evaluate's mapping classify the
                    # original error (fail-closed, Req 4.6).
                    await stack.aclose()
                    raise
                self._exit_stack = stack
            return self._client
