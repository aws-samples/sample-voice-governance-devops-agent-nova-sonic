"""Voice_Service exception hierarchy extending the shared ``PortalError`` base.

Defines the Voice_Service-specific branches of the portal exception
hierarchy (authentication, Bedrock streaming, DevOps Agent, guardrail,
session store, and task protection) and re-exports the shared classes from
``shared.exceptions`` (``PortalError``, ``ConfigurationError``, and the
notification-delivery branch), so the complete design hierarchy is
importable from ``app.exceptions``:

- ``PortalError``
    - ``ConfigurationError``
    - ``AuthenticationError`` → ``TokenInvalidError`` / ``TokenExpiredError``
    - ``BedrockStreamError`` → ``StreamOpenError`` / ``SegmentationError``
    - ``DevOpsAgentError`` → ``AgentRequestError`` / ``AgentTimeoutError``
    - ``GuardrailUnavailableError``
    - ``SessionStoreError``
    - ``NotificationPublishError``
    - ``WebPushError`` → ``SubscriptionGoneError`` / ``PushDeliveryError``
    - ``TaskProtectionError``

``shared`` is the small common package at ``backend/shared``; the runtime
image copies ``backend/shared`` and ``backend/voice_service`` and puts both
``backend/`` and ``backend/voice_service`` on ``PYTHONPATH``, so ``shared``
and ``app`` resolve as top-level packages.

Portal code raises only specific classes from this hierarchy, never a bare
``Exception`` (Req 17.4), and catches specific classes everywhere except
top-level boundary handlers (Req 17.5). Each concrete class builds its own
diagnostic message from typed context, so call sites never format message
strings; messages carry key names and identifiers only, never secret
values.
"""

from typing import Final, Literal

from shared.exceptions import (
    ConfigurationError,
    NotificationPublishError,
    PortalError,
    PushDeliveryError,
    SubscriptionGoneError,
    WebPushError,
)

__all__ = [
    "THROTTLE_ERROR_CODES",
    "AgentRequestError",
    "AgentTimeoutError",
    "AuthenticationError",
    "BedrockStreamError",
    "ConfigurationError",
    "DevOpsAgentError",
    "GuardrailUnavailableError",
    "NotificationPublishError",
    "PortalError",
    "PushDeliveryError",
    "SegmentationError",
    "SessionStoreError",
    "StreamOpenError",
    "SubscriptionGoneError",
    "TaskProtectionError",
    "TokenExpiredError",
    "TokenInvalidError",
    "WebPushError",
]

THROTTLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "RequestThrottled",
        "ServiceQuotaExceededException",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)
"""API error codes meaning "busy, try later" rather than "broken".

Kept here rather than in the adapter so orchestration can react to a
throttled agent without importing an adapter (the ports-and-adapters
boundary); the adapter records the raw code as
:attr:`AgentRequestError.detail` and this set interprets it.
"""


class AuthenticationError(PortalError):
    """Base class for Cognito JWT validation failures (Req 7.3).

    Concrete subclasses distinguish structurally invalid tokens
    (``TokenInvalidError``) from expired ones (``TokenExpiredError``), which
    map to the ``auth_invalid`` and ``auth_expired`` WebSocket error
    categories respectively.
    """


class TokenInvalidError(AuthenticationError):
    """A presented JWT failed structural validation (Req 7.3).

    Raised by the JWT validator when a token has a bad signature, a wrong
    issuer or audience, or is malformed or absent. The WebSocket handshake
    is rejected before the connection is accepted and no Voice_Session is
    created. The message never includes the token itself.

    Attributes:
        reason: Which validation check failed, for example ``"signature"``,
            ``"issuer"``, ``"audience"``, ``"malformed"``, or ``"missing"``.
    """

    def __init__(self, reason: str) -> None:
        """Initialize the error with the failed validation check.

        Args:
            reason: Which validation check failed, for example
                ``"signature"``, ``"issuer"``, ``"audience"``,
                ``"malformed"``, or ``"missing"``. Must never contain the
                token value.
        """
        self.reason = reason
        super().__init__(f"JWT validation failed: {reason}")


class TokenExpiredError(AuthenticationError):
    """A presented JWT is expired (Req 7.3, 7.6).

    Raised at the WebSocket handshake when the token is already expired,
    and mid-session by the token-expiry watchdog, after which the server
    sends an ``auth_expired`` error frame, closes the connection, and drops
    audio received after the expiry instant.

    Attributes:
        phase: Where the expiry was detected: ``"handshake"`` or
            ``"mid-session"``.
    """

    def __init__(self, phase: Literal["handshake", "mid-session"] = "handshake") -> None:
        """Initialize the error with the detection phase.

        Args:
            phase: Where the expiry was detected: ``"handshake"`` for a
                token already expired when presented, ``"mid-session"``
                when the watchdog detects expiry on a live session.
        """
        self.phase = phase
        super().__init__(f"JWT expired ({phase})")


class BedrockStreamError(PortalError):
    """Base class for Bedrock bidirectional stream failures.

    Concrete subclasses separate failures to open a stream
    (``StreamOpenError``) from failures of the segmentation rollover
    (``SegmentationError``).
    """


class StreamOpenError(BedrockStreamError):
    """A Bedrock_Stream could not be opened (Req 1.6).

    Raised by the Bedrock stream adapter when
    ``InvokeModelWithBidirectionalStream`` fails to establish a stream. The
    session manager sends a ``bedrock_unavailable`` error frame, closes the
    WebSocket, and ends the session without forwarding further audio.

    Attributes:
        detail: Short description of the underlying open failure, when
            known.
    """

    def __init__(self, detail: str | None = None) -> None:
        """Initialize the error with an optional failure description.

        Args:
            detail: Short description of the underlying open failure, for
                example the SDK error code; ``None`` when no further detail
                is available.
        """
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"Failed to open Bedrock stream{suffix}")


class SegmentationError(BedrockStreamError):
    """A session segmentation rollover failed (Req 2.6).

    Raised when the replacement stream cannot be established or the
    context replay does not complete within the 10-second watchdog. The
    session manager persists the partial transcript, sends a
    ``segmentation_failed`` error frame, and logs the failure with the
    session identifier.

    Attributes:
        session_id: Identifier of the Voice_Session whose rollover failed.
        reason: Short description of the rollover failure, for example
            ``"watchdog timeout"`` or ``"replacement stream open failed"``.
    """

    def __init__(self, session_id: str, reason: str) -> None:
        """Initialize the error with the session and failure reason.

        Args:
            session_id: Identifier of the Voice_Session whose rollover
                failed.
            reason: Short description of the rollover failure, for example
                ``"watchdog timeout"`` or ``"replacement stream open
                failed"``.
        """
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"Segmentation failed for session {session_id!r}: {reason}")


class DevOpsAgentError(PortalError):
    """Base class for DevOps Agent call failures.

    Concrete subclasses separate request failures (``AgentRequestError``)
    from exhaustion of the streaming time budget (``AgentTimeoutError``).
    Both are converted into error tool results so Nova Sonic can verbalize
    the failure to the engineer.
    """


class AgentRequestError(DevOpsAgentError):
    """A DevOps Agent API call failed (Req 3.7).

    Raised by the DevOps Agent adapter when ``CreateChat`` or
    ``SendMessage`` fails. The tool router returns an error tool result and
    logs the failure with the exception class and session identifier.

    Attributes:
        operation: The failing agent API operation, ``"CreateChat"`` or
            ``"SendMessage"``.
        detail: Service-reported failure description when one was
            available — an API error code such as
            ``ServiceQuotaExceededException``, or a ``responseFailed``
            event's error code and message — else ``None``.
    """

    def __init__(
        self,
        operation: Literal["CreateChat", "SendMessage"],
        detail: str | None = None,
    ) -> None:
        """Initialize the error with the failing agent operation.

        Args:
            operation: The failing agent API operation, ``"CreateChat"``
                or ``"SendMessage"``.
            detail: Service-reported failure description to carry in the
                message, when the response stream provided one.
        """
        self.operation = operation
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"DevOps Agent {operation} failed{suffix}")

    @property
    def is_throttled(self) -> bool:
        """Report whether the failure was the agent refusing load.

        Distinguishing "busy, retry later" from "broken" matters because
        the two need opposite reactions: a throttled request should back
        off, while retrying it immediately — as Nova_Sonic does when the
        tool result reads like a generic failure — deepens the quota
        exhaustion that caused it.

        Returns:
            ``True`` when :attr:`detail` names a throttling or quota error
            code (see :data:`THROTTLE_ERROR_CODES`).
        """
        if self.detail is None:
            return False
        return any(code in self.detail for code in THROTTLE_ERROR_CODES)


class AgentTimeoutError(DevOpsAgentError):
    """A DevOps Agent response exceeded its stream budget (Req 3.8).

    Raised when consuming the agent's streamed response exceeds the
    configured budget (60 seconds by design). The tool router stops
    consuming the stream, returns an error tool result, and logs the
    timeout with the session identifier.

    Attributes:
        timeout_seconds: The exceeded time budget in seconds.
    """

    def __init__(self, timeout_seconds: float) -> None:
        """Initialize the error with the exceeded time budget.

        Args:
            timeout_seconds: The time budget in seconds that the streamed
                response exceeded.
        """
        self.timeout_seconds = timeout_seconds
        super().__init__(f"DevOps Agent response exceeded {timeout_seconds:g}s budget")


class GuardrailUnavailableError(PortalError):
    """A guardrail evaluation could not be completed (Req 4.6).

    Raised by the guardrail adapter on SDK errors, timeouts, throttles, or
    malformed responses from ``ApplyGuardrail``. The guardrail gate is
    fail-closed: this error is always treated as a BLOCK decision and the
    DevOps Agent is never called.

    Attributes:
        reason: Short description of the evaluation failure, for example
            ``"timeout"``, ``"throttled"``, or ``"malformed response"``.
    """

    def __init__(self, reason: str = "evaluation failed") -> None:
        """Initialize the error with the evaluation failure reason.

        Args:
            reason: Short description of the evaluation failure, for
                example ``"timeout"``, ``"throttled"``, or ``"malformed
                response"``.
        """
        self.reason = reason
        super().__init__(f"Guardrail unavailable: {reason}")


class SessionStoreError(PortalError):
    """A Session_Store read or write failed (Req 8.5).

    Raised by the DynamoDB store adapter when a read, conditional write,
    or transactional write fails. Writes are conditional single-item
    operations, so a failure leaves no partial record; transcript writes
    are retried under the shared bounded retry policy, and failures are
    logged with the session identifier.

    Attributes:
        operation: Short description of the failing store operation, for
            example ``"put session"`` or ``"read transcript"``.
        session_id: Identifier of the Voice_Session involved, when the
            operation is session-scoped; ``None`` otherwise.
    """

    def __init__(self, operation: str, session_id: str | None = None) -> None:
        """Initialize the error with the failing operation and session.

        Args:
            operation: Short description of the failing store operation,
                for example ``"put session"`` or ``"read transcript"``.
            session_id: Identifier of the Voice_Session involved, when the
                operation is session-scoped; ``None`` otherwise.
        """
        self.operation = operation
        self.session_id = session_id
        scope = f" for session {session_id!r}" if session_id else ""
        super().__init__(f"Session store operation {operation!r} failed{scope}")


class TaskProtectionError(PortalError):
    """An ECS task protection state change failed (Req 10.7).

    Raised by the task protection adapter when acquiring, refreshing, or
    releasing scale-in protection fails. The protection manager retries
    under the shared bounded retry policy and, on exhaustion, flips the
    ``/healthz`` readiness flag to 503 until protection is confirmed.

    Attributes:
        operation: The failing protection transition, ``"acquire"`` or
            ``"release"`` (rolling expiry refreshes are acquires).
    """

    def __init__(self, operation: Literal["acquire", "release"]) -> None:
        """Initialize the error with the failing protection transition.

        Args:
            operation: The failing protection transition, ``"acquire"`` or
                ``"release"`` (rolling expiry refreshes are acquires).
        """
        self.operation = operation
        super().__init__(f"Task protection {operation} failed")
