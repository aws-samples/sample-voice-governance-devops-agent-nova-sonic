"""Optional SNS escalation channel for the Notifier Lambda (Req 5.11).

Publishes each normalized Incident_Notification to the escalation SNS
topic **when escalation is configured**: Req 5.11 is a WHERE clause, so
the topic ARN is optional configuration. A publisher constructed without
a topic ARN (``None``, empty, or whitespace-only) represents the
escalation-not-configured state and its :meth:`SnsPublisher.publish` is a
documented no-op that touches no AWS client at all.

**Payload.** The SNS ``Message`` is the JSON serialization of the
notification's wire payload (``IncidentNotification.to_payload``) — the
identical Incident_Notification schema published to AppSync Events and
Web Push (design: Incident_Notification payload schema). The ``Subject``
is the incident summary rendered through :func:`_subject_for`, which
sanitizes and truncates it leniently to satisfy the SNS constraint that a
subject be ASCII text without control characters, at most 100 characters,
beginning with a letter, number, or punctuation mark: every character
outside printable ASCII becomes a space, the result is trimmed and cut to
100 characters, and a summary that sanitizes to nothing falls back to a
constant subject.

**Single attempt, no retry.** The requirements map bounded retries to the
AppSync channel (Req 5.9, 5.10) and the Web Push channel (Req 6.8) only;
Req 5.11 mandates none for SNS. One publish attempt is made, and a
failure propagates so the Lambda handler's channel isolation records the
loss without suppressing the other channels.

**Failures.** Every SDK failure (``botocore``'s ``ClientError`` and
``BotoCoreError``) is logged and re-raised as
:class:`shared.exceptions.NotificationPublishError` with the SDK
exception chained as the cause. That shared class is reused here
deliberately: semantically it means "a notification channel publish
failed", and raising it lets the handler treat an escalation failure
uniformly with the other channels. Its ``status_code`` is ``None``
because SNS failures surface as SDK exceptions rather than raw HTTP
statuses. Log entries carry the notification identifier only — never
payload contents.

Alongside the other ``src.channels`` modules and ``src.subscription_repo``,
this is one of the Notifier's adapter modules — the only ones permitted to
import AWS SDKs (Req 17.6). All access uses ``aioboto3`` so the publish is
async end to end (Req 17.3), with one lazily created SNS client per
publisher instance, released by :meth:`SnsPublisher.aclose`.

**Encryption in transit (Req 12.7).** botocore resolves the SNS endpoint
to ``https://sns.<region>.amazonaws.com``; this adapter never overrides
``use_ssl`` or ``endpoint_url``, so every connection is TLS.
"""

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any, Final

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError
from shared.exceptions import NotificationPublishError

from src.normalizer import IncidentNotification

__all__ = ["SnsPublisher"]

_LOGGER: Final = logging.getLogger(__name__)

_SUBJECT_MAX_CHARS: Final = 100
"""Maximum SNS ``Subject`` length in characters."""

_FALLBACK_SUBJECT: Final = "Incident notification"
"""Subject used when a summary sanitizes to nothing (e.g. all non-ASCII)."""

_PUBLISH_FAILED_LOG_MESSAGE: Final = "SNS escalation publish failed"
"""Log message for a failed publish; context travels in extra fields only."""

_SKIPPED_LOG_MESSAGE: Final = "SNS escalation not configured; publish skipped"
"""Debug log message for the documented no-op path (Req 5.11 WHERE clause)."""

# The specific exception classes the SNS SDK surface can raise: ClientError
# covers service-reported errors (missing topics, access denied, throttles),
# BotoCoreError covers everything client-side (endpoint resolution,
# credentials, transport). Never a bare Exception (Req 17.5).
_SNS_ERRORS: Final = (BotoCoreError, ClientError)


def _subject_for(summary: str) -> str:
    """Render an incident summary as a valid SNS ``Subject``.

    Lenient sanitization for the SNS subject constraints (ASCII text, no
    line breaks or control characters, at most 100 characters, beginning
    with a letter, number, or punctuation mark): each character outside
    printable ASCII (``0x20``–``0x7E``) — control characters, line breaks,
    and all non-ASCII text — is replaced with a space, surrounding
    whitespace is trimmed so the subject starts with a printable non-space
    character, and the result is truncated to 100 characters.

    Args:
        summary: The notification's human-readable incident summary.

    Returns:
        The sanitized, truncated subject; the constant fallback subject
        when the summary sanitizes to nothing.
    """
    sanitized = "".join(ch if " " <= ch <= "~" else " " for ch in summary)
    trimmed = sanitized.strip()[:_SUBJECT_MAX_CHARS].rstrip()
    return trimmed or _FALLBACK_SUBJECT


def _publish_failure(notification_id: str) -> NotificationPublishError:
    """Log one failed publish attempt and build the error to raise.

    Must be called from within an ``except`` block: the entry is emitted
    with ``logging.exception`` so the active SDK exception's class and
    traceback are attached. The extra fields carry the notification
    identifier only — never payload contents.

    Args:
        notification_id: Identifier of the notification whose publish
            failed.

    Returns:
        The ``NotificationPublishError`` for the caller to raise, chained
        to the active exception via ``raise ... from``. Carries ``None``
        in place of an HTTP status (see the module docstring on the
        shared-class reuse).
    """
    _LOGGER.exception(
        _PUBLISH_FAILED_LOG_MESSAGE,
        extra={"notification_id": notification_id},
    )
    return NotificationPublishError(None)


class SnsPublisher:
    """Publishes Incident_Notifications to the escalation SNS topic.

    Notification channel used by the Notifier Lambda handler: when
    escalation is configured, each :meth:`publish` sends one
    ``sns:Publish`` carrying the notification's wire payload to the
    configured topic (Req 5.11); when it is not, :meth:`publish` is a
    documented no-op. One instance holds one lazily created aioboto3 SNS
    client shared by all publishes: the first configured publish creates
    it, and :meth:`aclose` releases the underlying HTTP connections when
    the Lambda invocation finishes with the instance.
    """

    def __init__(self, topic_arn: str | None, region: str) -> None:
        """Initialize the publisher with its topic ARN and region.

        No AWS connection is made here: the SNS client is created lazily
        by the first configured publish, so construction is side-effect
        free and safe outside a running event loop.

        Args:
            topic_arn: ARN of the escalation SNS topic from configuration.
                ``None``, empty, or whitespace-only means escalation is
                not configured (Req 5.11 WHERE clause): the publisher
                becomes a no-op and never creates an AWS client.
            region: AWS region hosting the topic (all connections use the
                regional TLS endpoint, Req 12.7).
        """
        normalized = topic_arn.strip() if topic_arn is not None else ""
        self._topic_arn = normalized or None
        self._region = region
        self._session = aioboto3.Session()
        self._exit_stack = AsyncExitStack()
        self._init_lock = asyncio.Lock()
        self._client: Any = None

    async def publish(self, notification: IncidentNotification) -> None:
        """Publish one Incident_Notification to the escalation topic.

        When escalation is not configured this is a no-op (Req 5.11
        WHERE clause): it returns immediately after a debug log entry,
        making no AWS call. Otherwise it makes exactly one publish
        attempt — no retry, per the module docstring — with the
        sanitized, truncated summary as ``Subject`` and the JSON wire
        payload as ``Message``.

        Args:
            notification: The normalized incident notification to
                escalate.

        Raises:
            NotificationPublishError: If the single publish attempt
                fails; logged with the notification identifier, with the
                SDK error as the cause, and carrying ``None`` in place of
                an HTTP status. The handler's channel isolation records
                it without suppressing the other channels.
        """
        if self._topic_arn is None:
            _LOGGER.debug(
                _SKIPPED_LOG_MESSAGE,
                extra={"notification_id": notification.notification_id},
            )
            return
        try:
            client = await self._ensure_client()
            await client.publish(
                TopicArn=self._topic_arn,
                Subject=_subject_for(notification.summary),
                Message=json.dumps(notification.to_payload()),
            )
        except _SNS_ERRORS as exc:
            raise _publish_failure(notification.notification_id) from exc

    async def aclose(self) -> None:
        """Release the underlying AWS connections.

        Closes the lazily created SNS client, after which the instance
        may be reused (the next configured publish recreates it).
        Teardown is best effort: a close failure is logged and suppressed
        so shutdown never fails on cleanup; closing an instance that
        never created a client, or closing twice, is a no-op.
        """
        self._client = None
        try:
            await self._exit_stack.aclose()
        except (BotoCoreError, ClientError, OSError):
            _LOGGER.exception("SNS publisher close failed")

    async def _ensure_client(self) -> Any:
        """Return the SNS client, creating it on first use.

        The client is entered on the instance's exit stack so
        :meth:`aclose` can release it; a lock serializes concurrent first
        calls so exactly one client is ever created. A creation failure
        leaves the instance unchanged, so a later call retries.

        Returns:
            The shared aioboto3 SNS client.

        Raises:
            BotoCoreError: If the SDK cannot construct the client;
                :meth:`publish` converts it to
                ``NotificationPublishError``.
        """
        async with self._init_lock:
            if self._client is None:
                self._client = await self._exit_stack.enter_async_context(
                    self._session.client("sns", region_name=self._region)
                )
        return self._client
