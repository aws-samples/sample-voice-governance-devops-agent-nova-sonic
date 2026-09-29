"""Lambda entrypoint fanning one incident event out to all channels (Req 5.1).

Composition root of the Notifier: each EventBridge invocation is normalized
into one Incident_Notification (``src.normalizer``) and delivered through
the AppSync Events channel, the Web Push channel, and the SNS escalation
channel in a single concurrent ``asyncio.gather`` — async I/O end to end
(Req 17.2, 17.3).

**Event loop per invocation (Req 17.2).** Lambda calls :func:`handler`
synchronously by its calling contract; the handler drives the fully async
pipeline with ``asyncio.run``, which creates a fresh event loop, runs
:func:`_handle` to completion, and closes the loop. Tradeoff: nothing
loop-bound survives between invocations — the aioboto3 and httpx clients
the channels create are opened and closed within one invocation, so a warm
container can never reuse a client bound to a closed loop — at the cost of
re-establishing connections on every invocation. For the Notifier's low,
bursty event rate that safety wins; the only cross-invocation state is
loop-free plain data (the memoized VAPID key below).

**Configuration (Req 14.4 style).** All configuration comes from Lambda
environment variables — the Notifier carries no Settings class; its
manifest is small enough for the frozen :class:`_NotifierConfig` dataclass.
Required keys: ``APPSYNC_EVENTS_HTTP_ENDPOINT``,
``SUBSCRIPTIONS_TABLE_NAME``, ``AWS_REGION`` (set automatically by the
Lambda runtime), ``VAPID_SUBJECT``, and ``VAPID_PRIVATE_KEY_SECRET_NAME``.
Optional: ``SNS_ESCALATION_TOPIC_ARN`` — absent means SNS escalation is not
configured (Req 5.11) and the SNS publisher is its documented no-op. A
missing or blank required key raises ``ConfigurationError`` naming the key,
failing the invocation — the desired signal for a misdeployed function.

**VAPID private key.** Sensitive values are referenced from the
environment by *name* only, mirroring the Voice_Service configuration
convention (Req 14.1): ``VAPID_PRIVATE_KEY_SECRET_NAME`` names an SSM
Parameter Store SecureString, fetched asynchronously via aioboto3
``get_parameter(WithDecryption=True)`` at cold start and memoized in a
module-level cache so warm invocations skip the fetch. The key is held as
a plain string, handed to the Web Push sender, and never logged.

**Channel isolation (Req 5.1).** The three channels run concurrently and
independently: each channel wrapper catches the channel's expected
failures (``NotificationPublishError``; the Web Push wrapper additionally
guards ``SubscriptionStoreError`` even though the sender never raises by
contract) and logs them, and the gather runs with
``return_exceptions=True`` so even an unexpected exception in one channel
never cancels, suppresses, or outlives the others.

**Boundary handling (Req 17.5).** The Lambda entrypoint is one of the
design's top-level boundary handlers, the only place broad exception
handling is permitted. Two nets implement it: ``return_exceptions=True``
inside the fan-out gather, and a broad catch wrapping :func:`_handle`'s
body that logs any unexpected exception and returns a failure response
instead of re-raising (see the EventBridge retry decision below).
``ConfigurationError`` is deliberately exempt from the broad catch: it is
raised before any channel is attempted, so re-raising cannot duplicate a
delivery, it fails the invocation loudly — the right signal for a
misdeployed function — and EventBridge's retry gives free recovery when a
transient SSM outage broke the VAPID key fetch.

**EventBridge retry decision.** A channel failure never re-raises out of
the handler — not even when every channel fails: raising makes EventBridge
retry the whole event, duplicating notifications on any channel that
already succeeded. The handler instead logs each failure (the structured
log is the operational record — EventBridge invokes the function
asynchronously and discards the response) and returns normally with the
failed channels named in the response payload; ``notificationId``
de-duplication on the frontend covers whatever redelivery still occurs.

**SDK imports.** ``handler.py`` is the composition root: the import-linter
contract in ``pyproject.toml`` confines AWS SDK imports away from the pure
``src.normalizer`` only, so the aioboto3 SSM fetch lives here alongside
the channel wiring (Req 17.6).

**Testing seam.** :func:`_handle` accepts an optional :class:`_Channels`
override carrying pre-built channel instances. Production passes nothing
and the channels are built from configuration (including the VAPID key
fetch); tests inject fakes to drive mixed channel outcomes through the
real isolation, gather, and teardown paths without AWS access or
monkeypatching.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError
from shared.exceptions import ConfigurationError, NotificationPublishError
from shared.logging import configure_logging

from src.channels.appsync_publisher import AppSyncPublisher
from src.channels.sns_publisher import SnsPublisher
from src.channels.webpush_sender import WebPushSender
from src.normalizer import IncidentNotification, normalize
from src.subscription_repo import SubscriptionRepository, SubscriptionStoreError

__all__ = ["handler"]

_LOGGER: Final = logging.getLogger(__name__)

_ENV_APPSYNC_ENDPOINT: Final = "APPSYNC_EVENTS_HTTP_ENDPOINT"
"""Environment key naming the AppSync Events HTTP publish endpoint."""

_ENV_TABLE_NAME: Final = "SUBSCRIPTIONS_TABLE_NAME"
"""Environment key naming the push-subscriptions DynamoDB table."""

_ENV_REGION: Final = "AWS_REGION"
"""Environment key naming the AWS region; set by the Lambda runtime."""

_ENV_VAPID_SUBJECT: Final = "VAPID_SUBJECT"
"""Environment key carrying the VAPID ``sub`` claim (a ``mailto:`` URI)."""

_ENV_VAPID_SECRET_NAME: Final = "VAPID_PRIVATE_KEY_SECRET_NAME"
"""Environment key naming (never containing) the VAPID private key secret."""

_ENV_SNS_TOPIC_ARN: Final = "SNS_ESCALATION_TOPIC_ARN"
"""Optional environment key naming the SNS escalation topic (Req 5.11)."""

_REQUIRED_ENV_KEYS: Final[tuple[str, ...]] = (
    _ENV_APPSYNC_ENDPOINT,
    _ENV_TABLE_NAME,
    _ENV_REGION,
    _ENV_VAPID_SUBJECT,
    _ENV_VAPID_SECRET_NAME,
)
"""Required-key manifest validated on every invocation (Req 14.4 style)."""

_CHANNEL_NAMES: Final[tuple[str, ...]] = ("appsync", "webpush", "sns")
"""Channel names in the exact order :func:`_fan_out` gathers the channels."""

_SECRET_RETRIEVAL_REASON: Final = "could not be retrieved"
"""``ConfigurationError`` reason for a failed secret fetch (Req 14.5)."""

_CHANNEL_FAILED_LOG_MESSAGE: Final = (
    "Notification channel failed; other channels unaffected"
)
"""Log message recording one isolated, expected channel failure."""

_UNEXPECTED_FAILURE_LOG_MESSAGE: Final = (
    "Notification channel failed unexpectedly; other channels unaffected"
)
"""Log message for a failure outside the channel's documented contract."""

_CLOSE_FAILED_LOG_MESSAGE: Final = "Notification channel cleanup failed"
"""Log message for a best-effort close that failed during teardown."""

_CONTENTLESS_DROP_LOG_MESSAGE: Final = (
    "Dropping DevOps Agent event that announces no incident content"
)
"""Log message for an agent event that is not a finding (see the predicate)."""


def _is_contentless_agent_event(notification: IncidentNotification) -> bool:
    """Report whether an event is a DevOps Agent event carrying no finding.

    The DevOps Agent EventBridge rule matches on ``source`` alone, because
    the service publishes no documented detail-type for findings — so the
    rule also catches the agent's ordinary lifecycle events, including the
    chat executions the Voice_Service itself creates on every engineer
    question. Those carry no summary or title, so they normalize to the
    generic ``"Incident from DevOps Agent"`` placeholder and were being
    pushed to engineers as incidents that do not exist anywhere in the
    agent's console: a self-inflicted notification loop, one push per voice
    question.

    Dropping is scoped to the agent source on purpose. A CloudWatch alarm
    or Incident Manager event reaches this Lambda only by matching a
    narrow rule pattern (alarm transition into ALARM; ``StartIncident``),
    so even without a title it reports a real incident and must still be
    delivered.

    Args:
        notification: The normalized notification for the current event.

    Returns:
        ``True`` when the event came from the DevOps Agent source and
        carried no usable summary, so no notification should be sent.
    """
    return (
        notification.source == "devops-agent-finding"
        and notification.summary_is_fallback
    )


_FANOUT_COMPLETE_LOG_MESSAGE: Final = "Notification fan-out complete"
"""Log message summarizing one finished invocation; the operational record."""

_MISCONFIGURED_LOG_MESSAGE: Final = (
    "Notifier is misconfigured; invocation fails before any channel runs"
)
"""Log message for a configuration failure re-raised out of the boundary."""

_UNHANDLED_LOG_MESSAGE: Final = (
    "Notification fan-out failed unexpectedly; returning failure response"
)
"""Log message for an exception absorbed by the boundary catch (Req 17.5)."""

_VAPID_KEY_CACHE: Final[dict[str, str]] = {}
"""VAPID private key memoized by secret name across warm invocations.

Plain, loop-free data — the only state that intentionally survives
``asyncio.run`` loop turnover between invocations (see module docstring).
"""


@dataclass(frozen=True, slots=True)
class _NotifierConfig:
    """Validated Notifier configuration for one invocation (Req 14.4 style).

    Built by :func:`_load_config` from the Lambda environment. Carries no
    secret values: the VAPID private key is referenced by secret *name*
    and fetched separately by :func:`_vapid_private_key`.

    Attributes:
        appsync_http_endpoint: Full URL of the AppSync Events HTTP publish
            endpoint.
        subscriptions_table_name: Name of the push-subscriptions DynamoDB
            table read by the Web Push fan-out.
        region: AWS region hosting the AppSync API, the table, the SSM
            parameter, and the optional SNS topic.
        vapid_subject: VAPID ``sub`` claim identifying the push sender.
        vapid_private_key_secret_name: Name (never the value) of the SSM
            SecureString parameter holding the VAPID private key.
        sns_escalation_topic_arn: ARN of the escalation SNS topic, or
            ``None`` when escalation is not configured (Req 5.11).
    """

    appsync_http_endpoint: str
    subscriptions_table_name: str
    region: str
    vapid_subject: str
    vapid_private_key_secret_name: str
    sns_escalation_topic_arn: str | None


def _require(environ: Mapping[str, str], key: str) -> str:
    """Return the value of a required key, rejecting absent or blank ones.

    Args:
        environ: Environment mapping to read.
        key: Required environment variable name.

    Returns:
        The value with surrounding whitespace stripped.

    Raises:
        ConfigurationError: If ``key`` is absent or blank, naming the key
            and never a value (Req 14.4 style).
    """
    value = environ.get(key, "").strip()
    if not value:
        raise ConfigurationError(key)
    return value


def _load_config(environ: Mapping[str, str]) -> _NotifierConfig:
    """Load and validate the Notifier configuration from the environment.

    Checks the required-key manifest (:data:`_REQUIRED_ENV_KEYS`) and reads
    the optional SNS escalation topic, treating an absent or blank value as
    escalation-not-configured (Req 5.11).

    Args:
        environ: Environment mapping to read, ``os.environ`` in the Lambda
            runtime; the smoke and unit seams pass plain dictionaries.

    Returns:
        The validated, immutable configuration for this invocation.

    Raises:
        ConfigurationError: If a required key is absent or blank, naming
            the first such key in manifest order; the message never
            contains a configuration value (Req 14.4 style).
    """
    values = {key: _require(environ, key) for key in _REQUIRED_ENV_KEYS}
    topic_arn = environ.get(_ENV_SNS_TOPIC_ARN, "").strip() or None
    return _NotifierConfig(
        appsync_http_endpoint=values[_ENV_APPSYNC_ENDPOINT],
        subscriptions_table_name=values[_ENV_TABLE_NAME],
        region=values[_ENV_REGION],
        vapid_subject=values[_ENV_VAPID_SUBJECT],
        vapid_private_key_secret_name=values[_ENV_VAPID_SECRET_NAME],
        sns_escalation_topic_arn=topic_arn,
    )


async def _read_ssm_parameter(name: str, region: str) -> str:
    """Read one decrypted SecureString parameter from SSM Parameter Store.

    One aioboto3 ``get_parameter(WithDecryption=True)`` call — async end to
    end (Req 17.3) — over the regional TLS endpoint botocore resolves
    (Req 12.7). The client lives only for this call: it must not outlive
    the invocation's event loop (see module docstring).

    Args:
        name: Name of the SecureString parameter to read.
        region: AWS region hosting the parameter.

    Returns:
        The decrypted parameter value.

    Raises:
        BotoCoreError: If the SDK fails client-side (endpoint resolution,
            credentials, transport).
        ClientError: If SSM rejects the call (missing parameter, access
            denied, throttling).
    """
    session = aioboto3.Session()
    async with session.client("ssm", region_name=region) as client:
        response = await client.get_parameter(Name=name, WithDecryption=True)
    return str(response["Parameter"]["Value"])


async def _vapid_private_key(secret_name: str, region: str) -> str:
    """Fetch the VAPID private key, memoized across warm invocations.

    The first invocation of a Lambda container fetches the key from SSM
    (cold start); the module-level cache then serves every warm invocation
    without further AWS calls. The key is returned as a plain string for
    the Web Push sender and is never logged (Req 14.6 spirit).

    Args:
        secret_name: Name of the SecureString parameter holding the key.
        region: AWS region hosting the parameter.

    Returns:
        The VAPID EC private key material.

    Raises:
        ConfigurationError: If the fetch fails, naming the secret key with
            reason ``"could not be retrieved"`` and never including a
            value, mirroring the Voice_Service startup contract
            (Req 14.5).
    """
    cached = _VAPID_KEY_CACHE.get(secret_name)
    if cached is not None:
        return cached
    try:
        value = await _read_ssm_parameter(secret_name, region)
    except (BotoCoreError, ClientError) as exc:
        raise ConfigurationError(secret_name, _SECRET_RETRIEVAL_REASON) from exc
    _VAPID_KEY_CACHE[secret_name] = value
    return value


@dataclass(frozen=True, slots=True)
class _Channels:
    """The notification channels and repository for one invocation.

    Groups everything :func:`_fan_out` delivers through and tears down.
    Production instances come from :func:`_build_channels`; tests pass a
    pre-built override into :func:`_handle` to drive mixed channel
    outcomes through the real isolation and teardown paths (the module
    docstring's testing seam).

    Attributes:
        appsync: The AppSync Events broadcast channel (Req 5.1).
        webpush: The Web Push fan-out channel (Req 6.2).
        sns: The SNS escalation channel, a no-op when escalation is not
            configured (Req 5.11).
        repo: The subscription repository backing ``webpush``; carried
            here so teardown can close it alongside the channels.
    """

    appsync: AppSyncPublisher
    webpush: WebPushSender
    sns: SnsPublisher
    repo: SubscriptionRepository


async def _build_channels(config: _NotifierConfig) -> _Channels:
    """Construct the production channel set from validated configuration.

    Awaits the VAPID private key (memoized SSM fetch) and wires the three
    channels plus the subscription repository. Construction itself makes
    no AWS connection — every underlying client is created lazily on
    first use — so a failure after this point never leaks connections.

    Args:
        config: The invocation's validated Notifier configuration.

    Returns:
        The wired channel set for one invocation.

    Raises:
        ConfigurationError: If the VAPID private key cannot be retrieved
            (Req 14.5 semantics), naming the secret key only.
    """
    vapid_key = await _vapid_private_key(
        config.vapid_private_key_secret_name, config.region
    )
    repo = SubscriptionRepository(config.subscriptions_table_name, config.region)
    return _Channels(
        appsync=AppSyncPublisher(config.appsync_http_endpoint, config.region),
        webpush=WebPushSender(vapid_key, config.vapid_subject, repo),
        sns=SnsPublisher(config.sns_escalation_topic_arn, config.region),
        repo=repo,
    )


async def _publish_appsync(
    publisher: AppSyncPublisher,
    notification: IncidentNotification,
) -> bool:
    """Run the AppSync Events channel, isolating its failure (Req 5.1).

    The publisher retries internally under the shared bounded retry policy
    and logs the distinct exhaustion record (Req 5.9, 5.10); this wrapper
    converts the final ``NotificationPublishError`` into a logged channel
    outcome so the failure never suppresses the other channels.

    Args:
        publisher: The AppSync Events channel for this invocation.
        notification: The normalized notification to broadcast.

    Returns:
        ``True`` when the publish succeeded; ``False`` when it failed
        after exhausting its retries (already logged).
    """
    try:
        await publisher.publish(notification)
    except NotificationPublishError:
        _LOGGER.exception(
            _CHANNEL_FAILED_LOG_MESSAGE,
            extra={
                "channel": "appsync",
                "notification_id": notification.notification_id,
            },
        )
        return False
    return True


async def _send_webpush(
    sender: WebPushSender,
    notification: IncidentNotification,
) -> bool:
    """Run the Web Push channel, isolating its failure (Req 6.2).

    ``WebPushSender.send_to_all`` is fully self-handling by contract — a
    listing failure skips the fan-out with a log entry and every
    per-subscription outcome is absorbed and logged inside the sender
    (Req 6.5, 6.8) — so on the expected path this wrapper only normalizes
    the channel's result shape for the handler's gather. For safety it
    still guards the channel's failure classes should one escape the
    sender's contract (``SubscriptionStoreError`` from the repository the
    sender reads, ``NotificationPublishError`` for uniformity with the
    other channels); anything else is a contract violation caught by the
    gather's ``return_exceptions=True`` net.

    Args:
        sender: The Web Push channel for this invocation.
        notification: The normalized notification to fan out.

    Returns:
        ``True`` when the fan-out ran (per-subscription failures are
        handled inside the sender and do not constitute a channel
        failure); ``False`` when a guarded failure escaped the sender
        (already logged here).
    """
    try:
        await sender.send_to_all(notification)
    except (NotificationPublishError, SubscriptionStoreError):
        _LOGGER.exception(
            _CHANNEL_FAILED_LOG_MESSAGE,
            extra={
                "channel": "webpush",
                "notification_id": notification.notification_id,
            },
        )
        return False
    return True


async def _publish_sns(
    publisher: SnsPublisher,
    notification: IncidentNotification,
) -> bool:
    """Run the SNS escalation channel, isolating its failure (Req 5.11).

    When escalation is not configured the publisher is its documented
    no-op and the channel trivially succeeds. Otherwise a failed publish
    (single attempt, already logged by the publisher) is converted into a
    logged channel outcome so it never suppresses the other channels.

    Args:
        publisher: The SNS escalation channel for this invocation.
        notification: The normalized notification to escalate.

    Returns:
        ``True`` when the publish succeeded or escalation is not
        configured; ``False`` when the publish failed (already logged).
    """
    try:
        await publisher.publish(notification)
    except NotificationPublishError:
        _LOGGER.exception(
            _CHANNEL_FAILED_LOG_MESSAGE,
            extra={
                "channel": "sns",
                "notification_id": notification.notification_id,
            },
        )
        return False
    return True


def _failed_channels(
    results: Sequence[bool | BaseException],
    notification_id: str,
) -> list[str]:
    """Map gather results onto the names of the channels that failed.

    Results arrive in :data:`_CHANNEL_NAMES` order. A ``False`` is an
    expected, already-logged channel failure; an exception instance is a
    failure outside the channel's documented contract, captured by the
    gather's ``return_exceptions=True`` boundary (Req 17.5) and logged
    here with its traceback.

    Args:
        results: Per-channel gather results, one per entry of
            :data:`_CHANNEL_NAMES`, in the same order.
        notification_id: Identifier of the notification this fan-out
            delivered, carried on the log entries.

    Returns:
        The names of the failed channels, in channel order; empty when
        every channel succeeded.
    """
    failed: list[str] = []
    for name, result in zip(_CHANNEL_NAMES, results, strict=True):
        if isinstance(result, BaseException):
            _LOGGER.error(
                _UNEXPECTED_FAILURE_LOG_MESSAGE,
                exc_info=result,
                extra={"channel": name, "notification_id": notification_id},
            )
            failed.append(name)
        elif not result:
            failed.append(name)
    return failed


async def _close_all(*closes: Awaitable[None]) -> None:
    """Close every channel and repository, isolating close failures.

    Teardown mirrors the fan-out's isolation: the closes run concurrently
    under ``return_exceptions=True`` so one failing close never prevents
    the others, and any failure is logged and suppressed — cleanup must
    never fail the invocation.

    Args:
        closes: The ``aclose`` awaitables of the invocation's channels and
            repository.
    """
    results = await asyncio.gather(*closes, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            _LOGGER.warning(_CLOSE_FAILED_LOG_MESSAGE, exc_info=result)


async def _fan_out(
    event: Mapping[str, object],
    channels: _Channels | None,
) -> dict[str, object]:
    """Normalize one EventBridge event and fan it out to all channels.

    The guarded body of :func:`_handle`: validate configuration and build
    the channels (unless a test injected an override), normalize the
    event with an injected identity and clock (``src.normalizer`` is
    pure), and run the three channels concurrently with per-channel
    isolation (Req 5.1). Channel failures are recorded, never re-raised
    (see the module docstring's EventBridge retry decision); the channels
    and the subscription repository are always closed, even if the gather
    itself fails.

    Args:
        event: The EventBridge event as delivered to the Lambda handler.
        channels: Optional pre-built channel set (the module docstring's
            testing seam); when ``None``, the production set is built
            from the environment, including the VAPID key fetch.

    Returns:
        The invocation response: ``statusCode`` 200, the generated
        ``notificationId``, and ``failedChannels`` naming any channel that
        failed (empty when all succeeded). EventBridge discards it — the
        structured log is the operational record.

    Raises:
        ConfigurationError: If a required environment key is missing or
            blank, or the VAPID private key cannot be retrieved; the
            invocation fails before any channel is attempted, signalling a
            misdeployed function.
    """
    if channels is None:
        channels = await _build_channels(_load_config(os.environ))
    notification = normalize(
        event,
        notification_id=str(uuid4()),
        now_iso=datetime.now(UTC).isoformat(),
    )
    if _is_contentless_agent_event(notification):
        _LOGGER.info(
            _CONTENTLESS_DROP_LOG_MESSAGE,
            extra={
                "notification_id": notification.notification_id,
                "source": notification.source,
                "detail_keys": sorted(notification.detail or {}),
            },
        )
        await _close_all(
            channels.appsync.aclose(),
            channels.repo.aclose(),
            channels.sns.aclose(),
        )
        return {
            "statusCode": 200,
            "notificationId": notification.notification_id,
            "failedChannels": [],
            "dropped": True,
        }
    try:
        # Order must match _CHANNEL_NAMES: ("appsync", "webpush", "sns").
        results = await asyncio.gather(
            _publish_appsync(channels.appsync, notification),
            _send_webpush(channels.webpush, notification),
            _publish_sns(channels.sns, notification),
            return_exceptions=True,
        )
    finally:
        await _close_all(
            channels.appsync.aclose(),
            channels.repo.aclose(),
            channels.sns.aclose(),
        )
    failed = _failed_channels(results, notification.notification_id)
    _LOGGER.info(
        _FANOUT_COMPLETE_LOG_MESSAGE,
        extra={
            "notification_id": notification.notification_id,
            "failed_channels": failed,
        },
    )
    return {
        "statusCode": 200,
        "notificationId": notification.notification_id,
        "failedChannels": failed,
    }


async def _handle(
    event: Mapping[str, object],
    channels: _Channels | None = None,
) -> dict[str, object]:
    """Run one invocation inside the Lambda's boundary handler (Req 17.5).

    Configures structured JSON logging (Req 19.4), then runs
    :func:`_fan_out` under the module's top-level boundary catch — the
    only place broad exception handling is permitted. An unexpected
    exception is logged with its traceback and converted into a failure
    response instead of re-raising, so EventBridge never redelivers an
    event whose channels may already have partially delivered (the module
    docstring's EventBridge retry decision). ``ConfigurationError`` alone
    re-raises: it precedes every channel attempt, so failing the
    invocation is duplicate-free and loudly signals a misdeployed
    function while letting EventBridge retry transient secret-fetch
    outages.

    Args:
        event: The EventBridge event as delivered to the Lambda handler.
        channels: Optional pre-built channel set (the module docstring's
            testing seam); ``None`` in production.

    Returns:
        The :func:`_fan_out` response on success. On an unexpected
        failure, ``statusCode`` 500 with ``notificationId`` ``None`` and
        every channel listed in ``failedChannels`` — the conservative
        reading, since the fan-out's true outcome is unknown; the logged
        traceback is the operational record.

    Raises:
        ConfigurationError: If a required environment key is missing or
            blank, or the VAPID private key cannot be retrieved; logged
            here, then re-raised to fail the invocation before any
            channel is attempted.
    """
    configure_logging()
    try:
        response = await _fan_out(event, channels)
    except ConfigurationError:
        _LOGGER.exception(_MISCONFIGURED_LOG_MESSAGE)
        raise
    # Broad catch permitted: the Lambda entrypoint is a top-level boundary
    # handler (Req 17.5) — anything unexpected is logged and answered,
    # never re-raised into an EventBridge redelivery that could duplicate
    # deliveries on channels that already succeeded. (BLE001 exempts the
    # handler because the exception is logged with its traceback.)
    except Exception:
        _LOGGER.exception(_UNHANDLED_LOG_MESSAGE)
        return {
            "statusCode": 500,
            "notificationId": None,
            "failedChannels": list(_CHANNEL_NAMES),
        }
    return response


def handler(event: dict[str, object], context: object) -> dict[str, object]:
    """Lambda entry point: run one notification fan-out (Req 5.1, 17.2).

    Synchronous by the Lambda calling contract; drives the fully async
    pipeline with ``asyncio.run``, which creates a fresh event loop for
    this invocation, runs :func:`_handle` to completion, and closes the
    loop (see the module docstring for the tradeoffs of one loop per
    invocation). :func:`_handle` carries the boundary catch (Req 17.5),
    so besides a configuration failure this entrypoint always answers
    with a response dict.

    Args:
        event: The EventBridge event, matched by one of the three incident
            rule patterns (CloudWatch Alarm, Incident Manager, DevOps
            Agent finding).
        context: The Lambda context object; unused, present to satisfy the
            handler signature.

    Returns:
        The invocation response from :func:`_handle`: ``statusCode`` 200
        with the generated ``notificationId`` and ``failedChannels``, or
        the boundary's ``statusCode`` 500 failure response.

    Raises:
        ConfigurationError: If the function is misdeployed — a required
            environment key is missing or blank, or the VAPID private key
            cannot be retrieved; raised before any channel is attempted.
    """
    return asyncio.run(_handle(event))
