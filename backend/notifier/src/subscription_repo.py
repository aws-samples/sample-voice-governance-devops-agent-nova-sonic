"""DynamoDB access to the push-subscriptions table for the Notifier fan-out.

Repository adapter over the ``{env}-push-subscriptions`` table of the design
data model (PK ``engineer_id`` — the Cognito ``sub`` — SK ``endpoint_hash``
— the SHA-256 hex digest of the push endpoint URL — and the ``subscription``
map attribute ``{endpoint, keys: {p256dh, auth}}``). The Notifier's Web Push
channel reads every registered Web_Push_Subscription through
:meth:`SubscriptionRepository.list_all` before a fan-out (Req 6.2) and
removes subscriptions the push service rejects as permanently gone through
:meth:`SubscriptionRepository.delete` (Req 6.5).

Alongside ``src.channels``, this module is one of the Notifier's adapter
modules — the only ones permitted to import AWS SDKs (Req 17.6); the
import-linter contract in ``pyproject.toml`` keeps the pure ``src.normalizer``
free of them. All access uses ``aioboto3`` so every DynamoDB call is async
end to end (Req 17.3), mirroring the Voice_Service's
``app/adapters/dynamodb_store.py``: one lazily created DynamoDB resource per
repository instance, released by :meth:`SubscriptionRepository.aclose`.

**Failures.** Every SDK failure (``botocore``'s ``ClientError`` and
``BotoCoreError``) is logged with the failing operation and re-raised as
:class:`SubscriptionStoreError` with the SDK exception chained as the cause.
``SubscriptionStoreError`` is module-local rather than part of
``shared.exceptions`` by the domain-local exception pattern: the shared
hierarchy holds only branches used by more than one backend component, and
only the Notifier raises and handles this one. Log and error messages never
include item contents (endpoints or key material) — operation names and
identifiers only.

**Encryption in transit (Req 12.7).** botocore resolves the DynamoDB
endpoint to ``https://dynamodb.<region>.amazonaws.com``; this adapter never
overrides ``use_ssl`` or ``endpoint_url``, so every connection is TLS.
"""

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Final

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError
from shared.exceptions import PortalError

__all__ = [
    "StoredSubscription",
    "SubscriptionRepository",
    "SubscriptionStoreError",
]

_LOGGER: Final = logging.getLogger(__name__)

# Operation names carried by SubscriptionStoreError and the failure log
# entries; constants so raise sites pass typed context instead of formatting
# message strings (codebase convention).
_OP_LIST_ALL: Final = "list subscriptions"
_OP_DELETE: Final = "delete subscription"

_FAILURE_LOG_MESSAGE: Final = "Subscription store operation failed"
"""Log message for failed operations; context travels in extra fields only."""

# The specific exception classes the DynamoDB SDK surface can raise:
# ClientError covers service-reported errors (throttles, missing tables),
# BotoCoreError covers everything client-side (endpoint resolution,
# credentials, transport). Never a bare Exception (Req 17.5).
_DYNAMODB_ERRORS: Final = (BotoCoreError, ClientError)


class SubscriptionStoreError(PortalError):
    """A push-subscriptions table read or write failed.

    Module-local ``PortalError`` branch (domain-local pattern — see the
    module docstring). The Web Push channel treats a failed
    :meth:`SubscriptionRepository.list_all` as a skipped fan-out and a
    failed :meth:`SubscriptionRepository.delete` as a logged, non-fatal
    cleanup miss; neither failure is retried here.

    Attributes:
        operation: Short description of the failing store operation.
    """

    def __init__(self, operation: str) -> None:
        """Initialize the error with the failing operation name.

        Args:
            operation: Short description of the failing store operation,
                for example ``"list subscriptions"``. Never contains item
                contents.
        """
        self.operation = operation
        super().__init__(f"Subscription store operation failed: {operation}")


@dataclass(frozen=True, slots=True)
class StoredSubscription:
    """One registered Web_Push_Subscription as persisted (Req 6.1).

    Immutable snapshot of a push-subscriptions table item, keyed like the
    table itself.

    Attributes:
        engineer_id: Cognito ``sub`` of the engineer who registered the
            subscription; the table's partition key.
        endpoint_hash: SHA-256 hex digest of the push endpoint URL; the
            table's sort key and the identifier used in delivery logs.
        subscription: The browser ``PushSubscription`` JSON as stored —
            ``{endpoint, keys: {p256dh, auth}}`` — passed verbatim to the
            Web Push library. Treated as sensitive: never logged.
    """

    engineer_id: str
    endpoint_hash: str
    subscription: Mapping[str, object]


def _failure(operation: str) -> SubscriptionStoreError:
    """Log one failed store operation and build the error to raise.

    Must be called from within an ``except`` block: the entry is emitted
    with ``logging.exception`` so the active SDK exception's class and
    traceback are attached. The message and extra fields carry the
    operation name only — never item contents.

    Args:
        operation: Short description of the failing store operation.

    Returns:
        The ``SubscriptionStoreError`` for the caller to raise, chained to
        the active exception via ``raise ... from``.
    """
    _LOGGER.exception(_FAILURE_LOG_MESSAGE, extra={"operation": operation})
    return SubscriptionStoreError(operation)


def _parse_item(item: Mapping[str, Any]) -> StoredSubscription:
    """Reconstruct a stored subscription from a push-subscriptions item.

    The key attributes and the ``subscription`` map are guaranteed by the
    table schema and the registration write path (the Voice_Service's
    ``put_subscription`` always writes all three).

    Args:
        item: The deserialized push-subscriptions table item.

    Returns:
        The ``StoredSubscription`` carrying the browser subscription
        mapping exactly as stored.
    """
    return StoredSubscription(
        engineer_id=str(item["engineer_id"]),
        endpoint_hash=str(item["endpoint_hash"]),
        subscription=item["subscription"],
    )


class SubscriptionRepository:
    """aioboto3-backed access to the push-subscriptions table.

    One instance holds one lazily created aioboto3 DynamoDB table accessor
    shared by all operations: the first operation creates it, and
    :meth:`aclose` releases the underlying HTTP connections when the
    Lambda invocation finishes with the instance. Construct it with the
    table name and region from the Notifier's configuration.
    """

    def __init__(self, table_name: str, region: str) -> None:
        """Initialize the repository with its table name and region.

        No AWS connection is made here: the DynamoDB resource is created
        lazily by the first operation, so construction is side-effect
        free and safe outside a running event loop.

        Args:
            table_name: Name of the push-subscriptions table.
            region: AWS region hosting the table (all connections use the
                regional TLS endpoint, Req 12.7).
        """
        self._table_name = table_name
        self._region = region
        self._session = aioboto3.Session()
        self._exit_stack = AsyncExitStack()
        self._init_lock = asyncio.Lock()
        self._table: Any = None

    async def _ensure_table(self) -> Any:
        """Return the table accessor, creating the resource on first use.

        The resource is entered on the instance's exit stack so
        :meth:`aclose` can release it; a lock serializes concurrent first
        calls so exactly one resource is ever created. A creation failure
        leaves the instance unchanged, so a later call retries.

        Returns:
            The shared aioboto3 ``Table`` accessor for the
            push-subscriptions table.

        Raises:
            BotoCoreError: If the SDK cannot construct the resource; the
                calling operation converts it to
                ``SubscriptionStoreError``.
        """
        async with self._init_lock:
            if self._table is None:
                resource = await self._exit_stack.enter_async_context(
                    self._session.resource("dynamodb", region_name=self._region)
                )
                self._table = await resource.Table(self._table_name)
        return self._table

    async def aclose(self) -> None:
        """Release the underlying AWS connections.

        Closes the lazily created DynamoDB resource, after which the
        instance may be reused (the next operation recreates it).
        Teardown is best effort: a close failure is logged and suppressed
        so shutdown never fails on cleanup; closing an instance that
        never created a resource, or closing twice, is a no-op.
        """
        self._table = None
        try:
            await self._exit_stack.aclose()
        except (BotoCoreError, ClientError, OSError):
            _LOGGER.exception("Subscription repository close failed")

    async def list_all(self) -> list[StoredSubscription]:
        """Read every registered Web_Push_Subscription (Req 6.2).

        A strongly consistent full-table ``Scan`` — the designed access
        pattern for the Notifier's fan-out over a bounded population of
        on-call engineers; the pagination loop follows
        ``LastEvaluatedKey`` so every page is read.

        Returns:
            Every registered subscription; an empty list when none are
            registered.

        Raises:
            SubscriptionStoreError: If the read fails; logged with the
                operation name, with the SDK error as the cause.
        """
        items: list[Any] = []
        try:
            table = await self._ensure_table()
            scan_kwargs: dict[str, Any] = {"ConsistentRead": True}
            while True:
                response = await table.scan(**scan_kwargs)
                items.extend(response.get("Items", ()))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_LIST_ALL) from exc
        return [_parse_item(item) for item in items]

    async def delete(self, engineer_id: str, endpoint_hash: str) -> None:
        """Remove one Web_Push_Subscription record (Req 6.5).

        One unconditional ``DeleteItem``: DynamoDB treats deleting an
        absent item as a success, so cleanup after a push-service
        rejection is idempotent and safe to repeat.

        Args:
            engineer_id: Cognito ``sub`` of the engineer who owns the
                subscription; the partition key.
            endpoint_hash: SHA-256 hex digest of the push endpoint URL;
                the sort key.

        Raises:
            SubscriptionStoreError: If the delete fails; logged with the
                operation name, with the SDK error as the cause.
        """
        try:
            table = await self._ensure_table()
            await table.delete_item(
                Key={"engineer_id": engineer_id, "endpoint_hash": endpoint_hash}
            )
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_DELETE) from exc
