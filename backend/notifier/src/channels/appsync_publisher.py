"""AppSync Events publish channel for the Notifier Lambda (Req 5.1, 5.9, 5.10).

Publishes each normalized Incident_Notification to the portal's AppSync
Events API over its HTTP publish endpoint — ``POST /event`` with the JSON
body ``{"channel": "/incidents/all", "events": ["<payload JSON>"]}`` — so
every browser subscribed to the broadcast Events_Channel receives the
incident within the notification latency budget (Req 5.1). Per the AppSync
Events wire contract the ``events`` field is an array of JSON-**string**
encoded events; this channel publishes exactly one event per request.

Requests are signed with AWS Signature Version 4 for the ``appsync``
service using the Lambda execution role's credentials: the Events API
authorizes publishes with IAM while browsers connect and subscribe with
Cognito (design: AppSync Events module, Req 7.4). Credential handling
keeps the event loop free of blocking I/O (Req 17.3):

* The botocore credential chain is resolved lazily once per publisher in
  a worker thread (``asyncio.to_thread``) — resolution may read config
  files or call IMDS/STS.
* Each publish then takes a frozen snapshot via
  ``credentials.get_frozen_credentials()``, also in a worker thread: for
  refreshable credentials (the Lambda role's rotating session
  credentials) that call may itself perform refresh I/O, and freezing
  yields a consistent access-key/secret/token triple for one signature.
* Signing itself (canonical request construction plus HMAC) is pure CPU
  and runs inline on the event loop.

One publish attempt maps every failure onto ``NotificationPublishError``:
a non-2xx response carries the HTTP status while transport-level failures
and an unresolvable (missing) credential chain carry ``None``. Attempts
run under the shared bounded retry policy (``shared.retry.retry_async``,
at most 3 retries so 4 attempts) which logs a warning per failure and one
distinct exhaustion record naming the exception class (Req 5.9, 5.10);
the final exception then propagates to the Lambda handler, whose
channel isolation ensures an exhausted AppSync publish never suppresses
Web Push or SNS delivery.
"""

import asyncio
import json
import logging
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import get_session
from shared.exceptions import NotificationPublishError
from shared.retry import retry_async

from src.normalizer import IncidentNotification

__all__ = ["AppSyncPublisher"]

_INCIDENTS_CHANNEL: Final = "/incidents/all"
"""Broadcast Events_Channel every portal browser subscribes to (Req 5.1)."""

_EVENT_PATH: Final = "/event"
"""Publish path of the AppSync Events HTTP endpoint."""

_SIGNING_SERVICE: Final = "appsync"
"""SigV4 signing service name for AppSync Events publishes."""

_CONTENT_TYPE: Final = "application/json"
"""Content type of the publish request body, included in the signature."""

_REQUEST_TIMEOUT_SECONDS: Final = 5.0
"""Per-request timeout for the lazily created client, bounding each attempt."""

_OPERATION_NAME: Final = "AppSyncPublisher.publish"
"""Operation name carried by the shared retry helper's log entries."""

# The specific exception classes one request can raise: httpx.HTTPError
# covers transport failures and timeouts, httpx.InvalidURL a malformed
# publish endpoint. Never a bare Exception (Req 17.5).
_HTTP_ERRORS: Final = (httpx.HTTPError, httpx.InvalidURL)

_LOGGER: Final = logging.getLogger(__name__)
"""Module logger receiving the retry helper's failure and exhaustion entries."""


def _normalized_event_url(http_endpoint: str) -> str:
    """Normalize the publish endpoint so it ends in exactly one ``/event``.

    Args:
        http_endpoint: Full URL (scheme included) of the AppSync Events
            HTTP endpoint, with or without the ``/event`` publish path
            and tolerant of a trailing slash.

    Returns:
        The endpoint URL terminated by a single ``/event`` path segment.
    """
    base = http_endpoint.rstrip("/")
    if base.endswith(_EVENT_PATH):
        return base
    return base + _EVENT_PATH


def _resolve_credentials() -> Any:
    """Resolve AWS credentials from the default botocore provider chain.

    Blocking — the chain may read configuration files or call IMDS/STS —
    so callers run it via ``asyncio.to_thread`` (Req 17.3).

    Returns:
        The resolved botocore credentials object, or ``None`` when the
        provider chain yields no credentials.
    """
    return get_session().get_credentials()


def _signed_headers(
    url: str,
    host: str,
    region: str,
    body: bytes,
    credentials: Any,
) -> dict[str, str]:
    """Build the SigV4-signed header set for one publish request.

    Pure CPU (canonical request construction and HMAC), so it is safe to
    run inline on the event loop. The ``host`` and ``Content-Type``
    headers are set before signing so both are covered by the signature.

    Args:
        url: The normalized ``/event`` publish URL being POSTed to.
        host: Host header value (the URL's network location).
        region: AWS region of the AppSync Events API, used in the
            credential scope.
        body: Exact request body bytes the signature must cover.
        credentials: Frozen credentials (access key, secret key, and
            optional session token) to sign with.

    Returns:
        All headers for the POST, including ``Authorization``,
        ``X-Amz-Date``, ``Content-Type``, ``host``, and — for session
        credentials — ``X-Amz-Security-Token``.
    """
    request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": _CONTENT_TYPE, "host": host},
    )
    SigV4Auth(credentials, _SIGNING_SERVICE, region).add_auth(request)
    return dict(request.headers)


class AppSyncPublisher:
    """Publishes Incident_Notifications to the AppSync Events channel.

    Notification channel used by the Notifier Lambda handler: each
    :meth:`publish` sends one signed ``POST /event`` carrying the
    notification's wire payload to the broadcast channel (default
    ``/incidents/all``), retrying under the shared bounded retry policy
    and letting the final failure propagate after the distinct
    exhaustion record is logged (Req 5.1, 5.9, 5.10).

    Transport uses a lazily created ``httpx.AsyncClient`` bound to a
    5-second per-request timeout; tests inject an
    ``httpx.MockTransport``-backed client instead, plus a static
    botocore credentials object to keep signing hermetic. Call
    :meth:`aclose` at shutdown to release an owned client; an injected
    client's lifecycle belongs to its owner.
    """

    def __init__(
        self,
        http_endpoint: str,
        region: str,
        *,
        channel: str = _INCIDENTS_CHANNEL,
        client: httpx.AsyncClient | None = None,
        credentials: Credentials | None = None,
    ) -> None:
        """Initialize the publisher against one AppSync Events API.

        Args:
            http_endpoint: Full URL (scheme included) of the Events API
                HTTP endpoint from configuration; accepted with or
                without the trailing ``/event`` publish path.
            region: AWS region hosting the Events API, used in the SigV4
                credential scope.
            channel: Events channel the notification is published to;
                defaults to the portal-wide broadcast channel
                ``/incidents/all`` (Req 5.1).
            client: Optional preconfigured ``httpx.AsyncClient`` to use
                instead of creating one — the seam tests use to supply an
                ``httpx.MockTransport``. An injected client's lifecycle
                belongs to its owner; :meth:`aclose` leaves it untouched.
            credentials: Optional botocore credentials object to sign
                with, bypassing provider-chain resolution — the seam
                tests use to supply static credentials. When omitted, the
                default chain is resolved lazily on first publish.
        """
        self._event_url = _normalized_event_url(http_endpoint)
        self._host = urlsplit(self._event_url).netloc
        self._region = region
        self._channel = channel
        self._client = client
        self._owns_client = client is None
        self._credentials = credentials

    async def publish(self, notification: IncidentNotification) -> None:
        """Publish one Incident_Notification to the Events_Channel.

        Serializes the notification's wire payload, wraps it in the
        AppSync Events publish body — ``{"channel": <channel>,
        "events": [<payload JSON string>]}``, exactly one event per
        request — and POSTs it signed to the ``/event`` endpoint.
        Attempts run under the shared bounded retry policy (at most 3
        retries, 4 attempts): each failure logs a warning and exhaustion
        logs the distinct record naming ``NotificationPublishError``
        (Req 5.9, 5.10), after which the last error propagates so the
        Lambda handler — which isolates channels — records the loss
        without suppressing Web Push or SNS delivery.

        Args:
            notification: The normalized incident notification to
                broadcast (Req 5.1).

        Raises:
            NotificationPublishError: When every attempt fails; carries
                the final HTTP status, or ``None`` for transport-level
                and credential failures.
        """
        body = json.dumps(
            {
                "channel": self._channel,
                "events": [json.dumps(notification.to_payload())],
            }
        ).encode("utf-8")

        async def attempt() -> None:
            """Run one signed publish attempt against the Events API."""
            await self._post_once(body)

        await retry_async(
            attempt,
            retryable=NotificationPublishError,
            retries=3,
            logger=_LOGGER,
            name=_OPERATION_NAME,
        )

    async def aclose(self) -> None:
        """Close the lazily created HTTP client, if one exists.

        Closes only a client this publisher created itself; an injected
        client's lifecycle belongs to its owner and is left untouched.
        Safe to call repeatedly — a later publish recreates the owned
        client on demand.
        """
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post_once(self, body: bytes) -> None:
        """Execute one signed publish attempt.

        Args:
            body: The serialized publish request body the signature must
                cover.

        Raises:
            NotificationPublishError: With the HTTP status when the
                Events API answers non-2xx, or with ``None`` when the
                request fails at the transport level or credentials
                cannot be resolved.
        """
        frozen = await self._frozen_credentials()
        headers = _signed_headers(
            self._event_url,
            self._host,
            self._region,
            body,
            frozen,
        )
        client = self._http_client()
        try:
            response = await client.post(
                self._event_url,
                content=body,
                headers=headers,
            )
        except _HTTP_ERRORS as exc:
            raise NotificationPublishError(None) from exc
        if not response.is_success:
            raise NotificationPublishError(response.status_code)

    async def _frozen_credentials(self) -> Any:
        """Return a frozen credential snapshot for signing one request.

        Resolves the botocore credential chain lazily on first use and
        caches the resolved object; every publish then freezes it anew so
        refreshable credentials stay valid across long-lived publisher
        instances. Both steps run in a worker thread because resolution
        and refresh may perform file, IMDS, or STS I/O (Req 17.3). A
        chain that yields no credentials is not cached, so a later
        attempt resolves again.

        Returns:
            The frozen credentials (access key, secret key, and optional
            session token) to sign with.

        Raises:
            NotificationPublishError: When the provider chain yields no
                credentials; carries ``None`` in place of an HTTP status.
        """
        if self._credentials is None:
            self._credentials = await asyncio.to_thread(_resolve_credentials)
        if self._credentials is None:
            raise NotificationPublishError(None)
        return await asyncio.to_thread(self._credentials.get_frozen_credentials)

    def _http_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating the owned one on first use.

        Returns:
            The injected client when one was provided, otherwise a
            lazily created ``httpx.AsyncClient`` bound to the module's
            per-request timeout so no attempt outlives the bounded retry
            budget.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
            )
        return self._client
