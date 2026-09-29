"""Shared ``PortalError`` exception base for the portal backend services.

Defines the root of the portal-wide exception hierarchy together with the
branches used by more than one backend component: configuration validation
errors (Voice_Service startup and Notifier configuration) and the
notification-delivery errors raised by the Notifier's AppSync Events and
Web Push channels.

Voice_Service-specific branches (authentication, Bedrock streaming, DevOps
Agent, guardrail, session store, and task protection) live in
``backend/voice_service/app/exceptions.py``, which imports this module and
re-exports it so the full design hierarchy is importable from
``app.exceptions``. This module is imported as the top-level package
``shared``; deployments place ``backend/`` (the common parent of ``shared``,
``voice_service``, and ``notifier``) on ``PYTHONPATH``.

Portal code raises only specific ``PortalError`` subclasses, never a bare
``Exception`` (Req 17.4), and catches specific classes everywhere except
top-level boundary handlers (Req 17.5). Each concrete class builds its own
diagnostic message from typed context, so call sites never format message
strings.
"""

__all__ = [
    "ConfigurationError",
    "NotificationPublishError",
    "PortalError",
    "PushDeliveryError",
    "SubscriptionGoneError",
    "WebPushError",
]


class PortalError(Exception):
    """Root of the portal exception hierarchy.

    Every exception raised by portal code is a subclass of ``PortalError``,
    which lets top-level boundary handlers (WebSocket connection handler,
    FastAPI exception middleware, Lambda entrypoint) convert portal failures
    into error frames or responses, while all other code catches only the
    specific subclass it can handle (Req 17.4, 17.5).
    """


class ConfigurationError(PortalError):
    """A required configuration key is missing or invalid (Req 14.4, 14.5).

    Raised while validating the required-key manifest at startup and when
    retrieval of a sensitive value from SSM Parameter Store or Secrets
    Manager fails. The message names the offending key only; it never
    contains a configuration or secret value.

    Attributes:
        key: Name of the configuration key that failed validation.
    """

    def __init__(self, key: str, reason: str = "is missing or invalid") -> None:
        """Initialize the error with the offending configuration key.

        Args:
            key: Name of the configuration key that failed validation.
            reason: Short failure description completing the sentence
                ``"Configuration key <key> ..."``, for example
                ``"could not be retrieved"``. Must never contain the
                configuration or secret value.
        """
        self.key = key
        super().__init__(f"Configuration key {key!r} {reason}")


class NotificationPublishError(PortalError):
    """Publishing an Incident_Notification to AppSync Events failed (Req 5.9).

    Raised by the Notifier's AppSync channel when the signed ``POST /event``
    request fails. The caller retries under the shared bounded retry policy
    and logs a distinct exhaustion record when retries run out (Req 5.10);
    the failure never suppresses the other notification channels.

    Attributes:
        status_code: HTTP status returned by the AppSync Events endpoint,
            when a response was received; ``None`` for transport-level
            failures.
    """

    def __init__(self, status_code: int | None = None) -> None:
        """Initialize the error with the failing HTTP status, when known.

        Args:
            status_code: HTTP status returned by the AppSync Events
                endpoint, when a response was received; ``None`` for
                transport-level failures.
        """
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"AppSync Events publish failed{status}")


class WebPushError(PortalError):
    """Base class for Web Push delivery failures.

    Concrete subclasses separate permanently gone subscriptions
    (``SubscriptionGoneError``, HTTP 404/410) from transient delivery
    failures (``PushDeliveryError``). The Notifier handles each subscription
    independently, so one failure never blocks deliveries to the rest.
    """


class SubscriptionGoneError(WebPushError):
    """The push service reports a subscription permanently gone (Req 6.5).

    Raised when a Web Push delivery is rejected with HTTP 404 or 410. The
    Notifier deletes the stored Web_Push_Subscription and stops further
    delivery attempts to that endpoint.

    Attributes:
        status_code: HTTP status returned by the push service (404 or 410).
        subscription_id: Identifier of the stored subscription, when known.
    """

    def __init__(self, status_code: int, subscription_id: str | None = None) -> None:
        """Initialize the error with the rejecting status and subscription.

        Args:
            status_code: HTTP status returned by the push service; 404 and
                410 mark the subscription as permanently gone.
            subscription_id: Identifier of the stored Web_Push_Subscription
                the delivery targeted, when known.
        """
        self.status_code = status_code
        self.subscription_id = subscription_id
        target = f" for subscription {subscription_id!r}" if subscription_id else ""
        super().__init__(f"Push subscription gone (HTTP {status_code}){target}")


class PushDeliveryError(WebPushError):
    """A Web Push delivery failed transiently (Req 6.8).

    Raised for delivery failures other than HTTP 404/410. The Notifier
    retries under the shared bounded retry policy and then discards the
    notification for that subscription only, leaving other subscriptions
    unaffected.

    Attributes:
        status_code: HTTP status returned by the push service, when a
            response was received; ``None`` for transport-level failures.
        subscription_id: Identifier of the stored subscription, when known.
    """

    def __init__(
        self,
        status_code: int | None = None,
        subscription_id: str | None = None,
    ) -> None:
        """Initialize the error with the failing status and subscription.

        Args:
            status_code: HTTP status returned by the push service, when a
                response was received; ``None`` for transport-level
                failures.
            subscription_id: Identifier of the stored Web_Push_Subscription
                the delivery targeted, when known.
        """
        self.status_code = status_code
        self.subscription_id = subscription_id
        status = f" (HTTP {status_code})" if status_code is not None else ""
        target = f" for subscription {subscription_id!r}" if subscription_id else ""
        super().__init__(f"Web push delivery failed{status}{target}")
