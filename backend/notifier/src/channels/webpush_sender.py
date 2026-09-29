"""Web Push delivery channel for the Notifier Lambda (Req 6.2, 6.5, 6.8).

Fans one Incident_Notification out to every registered
Web_Push_Subscription via ``pywebpush`` with VAPID authorization. The
delivered payload is the notification's wire payload
(``IncidentNotification.to_payload``), so it carries the incident summary
and, when the source event provided one, the ``executionId`` (Req 6.2); the
service worker renders the summary and scopes the Voice_Session it opens by
the ``executionId``.

**Blocking-call isolation (Req 17.3).** ``pywebpush`` performs synchronous
HTTP via ``requests``, so every delivery call runs inside
``asyncio.to_thread``; the event loop is never blocked, and concurrent
deliveries to different subscriptions proceed in parallel threads.

**Per-subscription outcome handling.** Each delivery is classified from the
``WebPushException`` the library raises (the push service's HTTP status is
extracted leniently from ``exc.response.status_code``):

- **404 / 410** — the push service reports the subscription permanently
  gone: the stored subscription is deleted and no further delivery attempts
  are made to it, with no retry (Req 6.5). A failure of the cleanup delete
  itself is logged and swallowed — a stale record only means one more
  rejected delivery on the next fan-out.
- **Any other status, or a transport-level failure** — the delivery is
  retried under the shared bounded retry policy (``shared.retry``, at most
  ``retries`` additional attempts with jittered exponential backoff); when
  retries are exhausted, the distinct exhaustion record is followed by a
  discard log entry and the notification is dropped for that one
  subscription (Req 6.8).

**Per-subscription isolation (Req 6.8).** :meth:`WebPushSender._send_one`
is fully self-handling — it never raises — so the ``asyncio.gather`` in
:meth:`WebPushSender.send_to_all` always awaits every delivery: one
subscription's failure can never block or suppress attempts to the rest.
A failure to list the subscriptions at all is logged and skips the fan-out
without raising, so the Web Push channel never breaks the Notifier's other
channels.

**Secrets and logging.** The VAPID private key is resolved from Secrets
Manager by the handler's configuration loading and passed in as a plain
string; it is never logged (log entries carry the endpoint hash and
notification id only, never key material, endpoints, or payload bodies).

**Testing seam.** The ``webpush_fn`` constructor parameter defaults to
``pywebpush.webpush`` and is injectable, so tests drive mixed delivery
outcomes with a plain fake callable — no monkeypatching; ``sleep`` is
likewise injectable to observe retries without real waiting.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from pywebpush import WebPushException, webpush
from shared.exceptions import PushDeliveryError, SubscriptionGoneError
from shared.retry import DEFAULT_RETRIES, retry_async

from src.normalizer import IncidentNotification
from src.subscription_repo import (
    StoredSubscription,
    SubscriptionRepository,
    SubscriptionStoreError,
)

__all__ = ["WebPushSender"]

_LOGGER: Final = logging.getLogger(__name__)

_GONE_STATUSES: Final = frozenset({404, 410})
"""Push-service statuses marking a subscription permanently gone (Req 6.5)."""

_LIST_FAILED_LOG_MESSAGE: Final = (
    "Web push fan-out skipped: listing subscriptions failed"
)
_GONE_LOG_MESSAGE: Final = "Push subscription gone; removing it"
_CLEANUP_FAILED_LOG_MESSAGE: Final = "Failed to remove gone push subscription"
_DISCARDED_LOG_MESSAGE: Final = (
    "Web push delivery retries exhausted; "
    "notification discarded for this subscription"
)


def _status_of(exc: object) -> int | None:
    """Extract the push service's HTTP status from a ``WebPushException``.

    Lenient by design: ``pywebpush`` attaches the ``requests`` response to
    the exception when one was received, but transport-level failures carry
    ``response=None`` and fakes may omit the attribute entirely.

    Args:
        exc: The exception raised by the Web Push library.

    Returns:
        The integer ``exc.response.status_code`` when present, else
        ``None``.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


class WebPushSender:
    """Fans Incident_Notifications out to Web_Push_Subscriptions.

    One instance is constructed per Notifier invocation with the VAPID
    material and the subscription repository; :meth:`send_to_all` performs
    one complete fan-out and never raises, so the Web Push channel runs
    safely alongside the Notifier's other channels.
    """

    def __init__(
        self,
        vapid_private_key: str,
        vapid_subject: str,
        repo: SubscriptionRepository,
        *,
        retries: int = DEFAULT_RETRIES,
        logger: logging.Logger | None = None,
        webpush_fn: Callable[..., object] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Initialize the sender with its VAPID material and collaborators.

        Args:
            vapid_private_key: The VAPID EC private key, resolved from
                Secrets Manager by the handler's configuration loading.
                Held as an opaque string and never logged.
            vapid_subject: The VAPID ``sub`` claim identifying the sender,
                for example ``"mailto:oncall@example.com"``. A fresh claims
                dict is built per delivery because ``pywebpush`` mutates
                the dict it is given.
            repo: Repository for reading and cleaning up stored
                Web_Push_Subscriptions.
            retries: Maximum retry count for transient delivery failures
                (Req 6.8); defaults to the shared policy's 3.
            logger: Logger receiving delivery, retry, and cleanup entries;
                this module's logger when omitted.
            webpush_fn: The delivery callable, invoked with the keyword
                arguments ``subscription_info``, ``data``,
                ``vapid_private_key``, and ``vapid_claims``. Defaults to
                ``pywebpush.webpush``; tests inject a fake to drive mixed
                outcomes without monkeypatching.
            sleep: Awaitable delay function forwarded to the shared retry
                helper; tests inject a recording fake to observe backoff
                without real waiting.
        """
        self._key = vapid_private_key
        self._subject = vapid_subject
        self._repo = repo
        self._retries = retries
        self._logger = logger if logger is not None else _LOGGER
        self._webpush_fn: Callable[..., object] = (
            webpush_fn if webpush_fn is not None else webpush
        )
        self._sleep = sleep

    async def send_to_all(self, notification: IncidentNotification) -> None:
        """Deliver one notification to every registered subscription.

        Reads all Web_Push_Subscriptions and delivers the notification's
        wire payload — carrying the summary and the ``executionId`` when
        present (Req 6.2) — to each of them concurrently. Never raises:
        a repository read failure logs and skips the fan-out, and every
        per-subscription outcome is fully handled inside
        :meth:`_send_one`, so one subscription's failure never prevents
        attempts to the rest (Req 6.8).

        Args:
            notification: The normalized Incident_Notification to fan out.
        """
        try:
            subscriptions = await self._repo.list_all()
        except SubscriptionStoreError:
            self._logger.exception(
                _LIST_FAILED_LOG_MESSAGE,
                extra={"notification_id": notification.notification_id},
            )
            return
        if not subscriptions:
            return
        data = json.dumps(notification.to_payload())
        await asyncio.gather(
            *(self._send_one(subscription, data) for subscription in subscriptions)
        )

    async def _send_one(self, subscription: StoredSubscription, data: str) -> None:
        """Deliver the payload to one subscription, handling every outcome.

        Fully self-handling — never raises (Req 6.8). Transient failures
        are retried under the shared bounded retry policy; a permanently
        gone subscription (HTTP 404/410) is deleted without any retry and
        receives no further deliveries (Req 6.5); exhausted retries end in
        a discard log entry for this subscription only.

        Args:
            subscription: The stored subscription to deliver to.
            data: The JSON-serialized notification payload.
        """
        try:
            await retry_async(
                lambda: self._attempt(subscription, data),
                retryable=PushDeliveryError,
                retries=self._retries,
                sleep=self._sleep,
                logger=self._logger,
                name=f"web push to {subscription.endpoint_hash}",
            )
        except SubscriptionGoneError as exc:
            self._logger.info(
                _GONE_LOG_MESSAGE,
                extra={
                    "endpoint_hash": subscription.endpoint_hash,
                    "status_code": exc.status_code,
                },
            )
            try:
                await self._repo.delete(
                    subscription.engineer_id, subscription.endpoint_hash
                )
            except SubscriptionStoreError:
                self._logger.exception(
                    _CLEANUP_FAILED_LOG_MESSAGE,
                    extra={"endpoint_hash": subscription.endpoint_hash},
                )
        except PushDeliveryError:
            # The shared retry helper has already emitted the distinct
            # exhaustion record naming the exception class; this entry
            # records the resulting discard decision (Req 6.8).
            self._logger.warning(
                _DISCARDED_LOG_MESSAGE,
                extra={"endpoint_hash": subscription.endpoint_hash},
            )

    async def _attempt(self, subscription: StoredSubscription, data: str) -> None:
        """Make one delivery attempt and classify its failure.

        Runs the synchronous Web Push call in a worker thread (Req 17.3)
        with a fresh VAPID claims dict (``pywebpush`` mutates the one it
        receives).

        Args:
            subscription: The stored subscription to deliver to.
            data: The JSON-serialized notification payload.

        Raises:
            SubscriptionGoneError: If the push service answered 404 or
                410 — the subscription is permanently gone (Req 6.5).
            PushDeliveryError: If the delivery failed with any other
                status or with a transport-level error (``requests``
                failures are ``OSError`` subclasses); retryable (Req 6.8).
        """
        try:
            await asyncio.to_thread(
                self._webpush_fn,
                subscription_info=dict(subscription.subscription),
                data=data,
                vapid_private_key=self._key,
                vapid_claims={"sub": self._subject},
            )
        except WebPushException as exc:
            status = _status_of(exc)
            if status is not None and status in _GONE_STATUSES:
                raise SubscriptionGoneError(
                    status, subscription.endpoint_hash
                ) from exc
            raise PushDeliveryError(status, subscription.endpoint_hash) from exc
        except OSError as exc:
            # requests' RequestException derives from IOError/OSError, so
            # this covers transport failures raised without a WebPush wrap.
            raise PushDeliveryError(None, subscription.endpoint_hash) from exc
