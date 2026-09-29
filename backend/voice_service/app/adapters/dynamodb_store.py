"""DynamoDB implementation of the Session_Store port (Req 8.1).

Concrete ``SessionStorePort`` (``ports.session_store``) over the four
Session_Store tables of the design data model, using ``aioboto3`` so every
DynamoDB call is async end to end. Adapters are the only modules permitted
to import AWS SDKs (Req 17.6, enforced by the import-linter contract in
``pyproject.toml``); table names and the region are injected from
``app.config.Settings`` by the application wiring.

**Tables (design Data Models).** One port method group per table:

- ``{env}-voice-sessions`` — Voice_Session snapshots keyed by
  ``session_id``; :meth:`DynamoDbSessionStore.get_session` /
  :meth:`DynamoDbSessionStore.put_session`. The table's ``by-engineer``
  GSI (PK ``engineer_id``, SK ``created_at``) is provisioned by the
  infrastructure layer for reconnect lookups but is not part of the port
  surface, so this adapter never queries it.
- ``{env}-agent-chats`` — DevOps_Agent chat mapping keyed by
  ``session_id``; :meth:`DynamoDbSessionStore.get_chat_id` /
  :meth:`DynamoDbSessionStore.put_chat_id` (Req 3.2).
- ``{env}-transcripts`` — transcript entries keyed by
  ``(session_id, seq)`` with the numeric ``seq`` sort key;
  :meth:`DynamoDbSessionStore.append_transcript` /
  :meth:`DynamoDbSessionStore.get_transcript` (Req 2.5, 8.3).
- ``{env}-push-subscriptions`` — Web_Push_Subscriptions keyed by
  ``(engineer_id, endpoint_hash)``;
  :meth:`DynamoDbSessionStore.put_subscription` /
  :meth:`DynamoDbSessionStore.delete_subscription` /
  :meth:`DynamoDbSessionStore.list_subscriptions` (Req 6.1). The
  full-table Scan behind ``list_subscriptions`` is the designed access
  pattern: the population is bounded (engineers on call).

**Write discipline (Req 8.5).** Every port mutation maps to exactly one
single-item write (a ``PutItem`` upsert or a ``DeleteItem``), and DynamoDB
single-item writes are atomic: the item is written (or deleted) entirely
or not at all, so a failed write leaves no partially updated record. No
port operation moves two items together, so the ``TransactWriteItems``
path the design reserves for multi-item moves is not needed by this
surface. Transcript writes are idempotent under the caller's bounded
retry (Req 2.7) because each entry is keyed by its ``(session_id, seq)``,
and deleting an absent subscription is a no-op, keeping cleanup
idempotent (Req 6.5).

**Vocabulary mappings.** Sessions are persisted via
``SessionState.store_status`` (``CONNECTING`` is stored as ``CREATED``)
and read back through the exact inverse mapping. Optional attributes
(``execution_id``, ``incident_context``) are omitted from the item when
absent, mirroring the data model's optional columns. Numeric attributes
(``seq``, ``segment_count``, ``ttl``) come back from the boto3 resource
layer as ``decimal.Decimal`` and are converted to ``int``. Reads use
strongly consistent reads so a record is visible immediately after the
write that persisted it, matching the persist-before-confirm discipline
(Req 8.2, 8.7) and the read-your-writes semantics of the in-memory fake.

**TTL (Req 8.4).** Session, chat-mapping, and transcript records carry
the ``ttl`` attribute (epoch seconds) that DynamoDB TTL expires
automatically. Callers compute the value with ``domain.ttl`` (the
record's last update time plus the configured retention period) and pass
it into each write; this adapter stores it verbatim. Subscription records
carry no TTL — they are removed explicitly on unsubscribe or
push-service rejection (Req 6.5). The bookkeeping
``created_at``/``updated_at`` attributes the data model marks optional on
chat and subscription records are omitted: no consumer reads them back,
and the port contract injects no clock.

**Failures (Req 8.5).** Every SDK failure (``botocore``'s ``ClientError``
and ``BotoCoreError``) is logged with the failing operation and, where
the operation is session-scoped, the affected Voice_Session identifier,
then re-raised as ``SessionStoreError`` carrying both, with the SDK
exception chained as the cause. Log and error messages never include item
contents (session snapshots, transcript text, or subscription material) —
operation names and identifiers only.

**Encryption in transit (Req 12.7).** botocore resolves the DynamoDB
endpoint to ``https://dynamodb.<region>.amazonaws.com``; this adapter
never overrides ``use_ssl`` or ``endpoint_url``, so every connection is
TLS with no plaintext fallback. Credentials come from the SDK's default
resolver chain (environment → profile → container credentials → IMDS),
which serves the Fargate task role in deployment.
"""

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from types import MappingProxyType
from typing import Any, Final

import aioboto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from app.domain.session import IncidentContext, SessionState, VoiceSession
from app.domain.transcript import Role, TranscriptEntry
from app.exceptions import SessionStoreError
from app.ports.session_store import SessionStorePort, WebPushSubscription

__all__ = ["DynamoDbSessionStore"]

_LOGGER: Final = logging.getLogger(__name__)

# Operation names carried by SessionStoreError and the failure log entries
# (Req 8.5); defined as constants so raise sites pass typed context instead
# of formatting message strings (codebase convention).
_OP_GET_SESSION: Final = "get session"
_OP_PUT_SESSION: Final = "put session"
_OP_GET_CHAT_ID: Final = "get chat id"
_OP_PUT_CHAT_ID: Final = "put chat id"
_OP_APPEND_TRANSCRIPT: Final = "append transcript"
_OP_GET_TRANSCRIPT: Final = "get transcript"
_OP_PUT_SUBSCRIPTION: Final = "put subscription"
_OP_DELETE_SUBSCRIPTION: Final = "delete subscription"
_OP_LIST_SUBSCRIPTIONS: Final = "list subscriptions"

_FAILURE_LOG_MESSAGE: Final = "Session store operation failed"
"""Log message for failed operations; context travels in extra fields only."""

_UNKNOWN_STATUS_LOG_MESSAGE: Final = "Session record has an unknown status"
"""Log message for a persisted ``status`` outside the store vocabulary."""

# The specific exception classes the DynamoDB SDK surface can raise:
# ClientError covers service-reported errors (conditional failures,
# throttles, missing tables), BotoCoreError covers everything client-side
# (endpoint resolution, credentials, transport). Never a bare Exception
# (Req 17.5).
_DYNAMODB_ERRORS: Final = (BotoCoreError, ClientError)

_STATE_BY_STORE_STATUS: Final[Mapping[str, SessionState]] = MappingProxyType(
    {state.store_status: state for state in SessionState}
)
"""Exact inverse of ``SessionState.store_status``: ``CREATED`` maps back to
``CONNECTING``; every other stored status maps back by name."""


def _failure(operation: str, session_id: str | None) -> SessionStoreError:
    """Log one failed store operation and build the error to raise (Req 8.5).

    Must be called from within an ``except`` block: the entry is emitted
    with ``logging.exception`` so the active SDK exception's class and
    traceback are attached. The message and extra fields carry the
    operation name and the Voice_Session identifier only — never item
    contents.

    Args:
        operation: Short description of the failing store operation, for
            example ``"put session"``.
        session_id: Identifier of the Voice_Session involved when the
            operation is session-scoped; ``None`` otherwise.

    Returns:
        The ``SessionStoreError`` for the caller to raise, chained to the
        active exception via ``raise ... from``.
    """
    extra: dict[str, object] = {"operation": operation}
    if session_id is not None:
        extra["session_id"] = session_id
    _LOGGER.exception(_FAILURE_LOG_MESSAGE, extra=extra)
    return SessionStoreError(operation, session_id)


def _session_item(session: VoiceSession, ttl: int) -> dict[str, Any]:
    """Build the voice-sessions table item for one Voice_Session snapshot.

    Maps the snapshot per the design data model: the state is stored via
    its ``store_status`` vocabulary, the optional ``execution_id`` and
    ``incident_context`` attributes are omitted when absent, and the
    caller-computed ``ttl`` is stored verbatim (Req 8.4).

    Args:
        session: The Voice_Session snapshot to persist.
        ttl: Time-to-live attribute value in epoch seconds.

    Returns:
        The JSON-ready item mapping for ``PutItem``.
    """
    item: dict[str, Any] = {
        "session_id": session.session_id,
        "engineer_id": session.engineer_id,
        "status": session.state.store_status,
        "segment_count": session.segment_count,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "ttl": ttl,
    }
    if session.execution_id is not None:
        item["execution_id"] = session.execution_id
    if session.incident_context is not None:
        item["incident_context"] = {
            "summary": session.incident_context.summary,
            "severity": session.incident_context.severity,
        }
    return item


def _parse_session(item: dict[str, Any]) -> VoiceSession:
    """Reconstruct a Voice_Session snapshot from a voice-sessions item.

    Inverts :func:`_session_item`: the stored ``status`` maps back through
    the exact inverse of ``SessionState.store_status`` (``CREATED`` →
    ``CONNECTING``), absent optional attributes become ``None``, and the
    ``Decimal`` the resource layer returns for ``segment_count`` is
    converted to ``int``.

    Args:
        item: The deserialized voice-sessions table item.

    Returns:
        The persisted ``VoiceSession`` snapshot.

    Raises:
        SessionStoreError: If the item's ``status`` is outside the stored
            vocabulary — corruption this adapter never writes; logged with
            the session identifier before raising (Req 8.5).
    """
    session_id = str(item["session_id"])
    status = str(item["status"])
    state = _STATE_BY_STORE_STATUS.get(status)
    if state is None:
        _LOGGER.error(_UNKNOWN_STATUS_LOG_MESSAGE, extra={"session_id": session_id})
        raise SessionStoreError(_OP_GET_SESSION, session_id)
    raw_context = item.get("incident_context")
    incident_context = (
        IncidentContext(
            summary=str(raw_context["summary"]),
            severity=str(raw_context["severity"]),
        )
        if raw_context is not None
        else None
    )
    execution_id = item.get("execution_id")
    return VoiceSession(
        session_id=session_id,
        engineer_id=str(item["engineer_id"]),
        created_at=str(item["created_at"]),
        updated_at=str(item["updated_at"]),
        state=state,
        execution_id=str(execution_id) if execution_id is not None else None,
        incident_context=incident_context,
        segment_count=int(item["segment_count"]),
    )


def _transcript_item(
    session_id: str, entry: TranscriptEntry, ttl: int
) -> dict[str, Any]:
    """Build the transcripts table item for one transcript entry.

    Args:
        session_id: Identifier of the owning Voice_Session (partition key).
        entry: The transcript line; its ``seq`` becomes the numeric sort
            key (Req 2.5).
        ttl: Time-to-live attribute value in epoch seconds (Req 8.4).

    Returns:
        The JSON-ready item mapping for ``PutItem``.
    """
    return {
        "session_id": session_id,
        "seq": entry.seq,
        "role": entry.role.value,
        "text": entry.text,
        "timestamp": entry.timestamp,
        "ttl": ttl,
    }


def _parse_transcript_entry(item: dict[str, Any]) -> TranscriptEntry:
    """Reconstruct a transcript entry from a transcripts table item.

    Args:
        item: The deserialized transcripts table item; the resource
            layer's ``Decimal`` sort key is converted back to ``int``.

    Returns:
        The persisted ``TranscriptEntry``.
    """
    return TranscriptEntry(
        seq=int(item["seq"]),
        role=Role(str(item["role"])),
        text=str(item["text"]),
        timestamp=str(item["timestamp"]),
    )


def _parse_subscription(item: dict[str, Any]) -> WebPushSubscription:
    """Reconstruct a Web_Push_Subscription from a push-subscriptions item.

    Args:
        item: The deserialized push-subscriptions table item.

    Returns:
        The persisted ``WebPushSubscription`` carrying the browser
        subscription mapping exactly as stored.
    """
    return WebPushSubscription(
        engineer_id=str(item["engineer_id"]),
        endpoint_hash=str(item["endpoint_hash"]),
        subscription=item["subscription"],
    )


class DynamoDbSessionStore(SessionStorePort):
    """aioboto3-backed ``SessionStorePort`` over the four Session_Store tables.

    One instance holds one lazily created aioboto3 DynamoDB service
    resource shared by all operations: the first operation creates it
    (and per-table accessors are cached thereafter), and :meth:`aclose`
    releases the underlying HTTP connections on application shutdown.
    Construct it with the table names and region from
    ``app.config.Settings``. Failure behavior is uniform across all nine
    port methods: SDK errors are logged and re-raised as
    ``SessionStoreError`` carrying the operation name and, where the
    operation is session-scoped, the Voice_Session identifier (Req 8.5).
    """

    def __init__(
        self,
        sessions_table: str,
        chats_table: str,
        subscriptions_table: str,
        transcripts_table: str,
        region: str,
    ) -> None:
        """Initialize the store with its table names and region.

        No AWS connection is made here: the DynamoDB resource is created
        lazily by the first operation, so construction is side-effect
        free and safe outside a running event loop.

        Args:
            sessions_table: Name of the voice-sessions table.
            chats_table: Name of the agent-chats table.
            subscriptions_table: Name of the push-subscriptions table.
            transcripts_table: Name of the transcripts table.
            region: AWS region hosting the tables (all connections use
                the regional TLS endpoint, Req 12.7).
        """
        self._sessions_table = sessions_table
        self._chats_table = chats_table
        self._subscriptions_table = subscriptions_table
        self._transcripts_table = transcripts_table
        self._region = region
        self._session = aioboto3.Session()
        self._exit_stack = AsyncExitStack()
        self._init_lock = asyncio.Lock()
        self._resource: Any = None
        self._tables: dict[str, Any] = {}

    async def _ensure_resource(self) -> Any:
        """Return the DynamoDB service resource, creating it on first use.

        The resource is entered on the instance's exit stack so
        :meth:`aclose` can release it; a lock serializes concurrent first
        calls so exactly one resource is ever created. A creation failure
        leaves the instance unchanged, so a later call retries.

        Returns:
            The shared aioboto3 DynamoDB service resource.

        Raises:
            BotoCoreError: If the SDK cannot construct the resource; the
                calling operation converts it to ``SessionStoreError``.
        """
        async with self._init_lock:
            if self._resource is None:
                self._resource = await self._exit_stack.enter_async_context(
                    self._session.resource("dynamodb", region_name=self._region)
                )
        return self._resource

    async def _table(self, name: str) -> Any:
        """Return the accessor for one table, cached after first use.

        Args:
            name: Name of the DynamoDB table.

        Returns:
            The aioboto3 ``Table`` accessor for ``name``.

        Raises:
            BotoCoreError: If the SDK cannot construct the resource; the
                calling operation converts it to ``SessionStoreError``.
        """
        table = self._tables.get(name)
        if table is None:
            resource = await self._ensure_resource()
            table = await resource.Table(name)
            self._tables[name] = table
        return table

    async def aclose(self) -> None:
        """Release the underlying AWS connections.

        Closes the lazily created DynamoDB resource and clears the table
        cache, after which the instance may be reused (the next operation
        recreates the resource). Teardown is best effort: a close failure
        is logged and suppressed so shutdown never fails on cleanup;
        closing an instance that never created a resource, or closing
        twice, is a no-op.
        """
        self._tables.clear()
        self._resource = None
        try:
            await self._exit_stack.aclose()
        except (BotoCoreError, ClientError, OSError):
            _LOGGER.exception("Session store close failed")

    async def get_session(self, session_id: str) -> VoiceSession | None:
        """Read one Voice_Session record from the voice-sessions table.

        Uses a strongly consistent ``GetItem`` so a snapshot persisted by
        :meth:`put_session` is immediately visible to a reconnect lookup
        (Req 8.3).

        Args:
            session_id: Identifier of the Voice_Session to read.

        Returns:
            The persisted ``VoiceSession`` snapshot, or ``None`` when no
            record exists — never created, expired by TTL, or already
            removed (Req 8.6).

        Raises:
            SessionStoreError: If the read fails or the record's status
                is unreadable; logged with the session identifier
                (Req 8.5).
        """
        try:
            table = await self._table(self._sessions_table)
            response = await table.get_item(
                Key={"session_id": session_id}, ConsistentRead=True
            )
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_GET_SESSION, session_id) from exc
        item = response.get("Item")
        if item is None:
            return None
        return _parse_session(item)

    async def put_session(self, session: VoiceSession, *, ttl: int) -> None:
        """Persist one Voice_Session snapshot to the voice-sessions table.

        One ``PutItem`` upsert of the full record. DynamoDB single-item
        writes are atomic — the item is replaced entirely or not at all —
        so a failure leaves no partially updated record (Req 8.5). The
        caller reports the state change only after this returns (Req 8.2).

        Args:
            session: The Voice_Session snapshot to persist, stored via
                its ``state.store_status`` representation.
            ttl: Time-to-live attribute value in epoch seconds, computed
                by ``domain.ttl`` from the snapshot's ``updated_at``
                (Req 8.4).

        Raises:
            SessionStoreError: If the write fails; logged with the
                session identifier, and no partial record remains
                (Req 8.5).
        """
        item = _session_item(session, ttl)
        try:
            table = await self._table(self._sessions_table)
            await table.put_item(Item=item)
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_PUT_SESSION, session.session_id) from exc

    async def get_chat_id(self, session_id: str) -> str | None:
        """Read the DevOps_Agent chat identifier mapped to a session.

        Uses a strongly consistent ``GetItem`` on the agent-chats table
        so a mapping persisted by :meth:`put_chat_id` is immediately
        reusable (Req 3.9).

        Args:
            session_id: Identifier of the Voice_Session whose chat
                mapping to read.

        Returns:
            The persisted chat identifier, or ``None`` when no mapping
            exists (Req 3.10).

        Raises:
            SessionStoreError: If the read fails; logged with the session
                identifier (Req 8.5).
        """
        try:
            table = await self._table(self._chats_table)
            response = await table.get_item(
                Key={"session_id": session_id}, ConsistentRead=True
            )
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_GET_CHAT_ID, session_id) from exc
        item = response.get("Item")
        if item is None:
            return None
        return str(item["chat_id"])

    async def put_chat_id(
        self,
        session_id: str,
        chat_id: str,
        *,
        ttl: int,
        execution_id: str | None = None,
    ) -> None:
        """Persist the DevOps_Agent chat mapping for a session.

        One ``PutItem`` upsert on the agent-chats table (one chat per
        session, Req 3.2); the optional ``execution_id`` attribute is
        omitted for an unscoped chat (Req 3.6).

        Args:
            session_id: Identifier of the owning Voice_Session; the
                mapping's partition key.
            chat_id: The chat identifier returned by
                ``aidevops:CreateChat``.
            ttl: Time-to-live attribute value in epoch seconds, aligned
                with the owning session's TTL (Req 8.4).
            execution_id: The execution the chat was scoped to at
                creation (Req 3.5), or ``None`` for an unscoped chat.

        Raises:
            SessionStoreError: If the write fails; logged with the
                session identifier, and no partial record remains
                (Req 8.5).
        """
        item: dict[str, Any] = {
            "session_id": session_id,
            "chat_id": chat_id,
            "ttl": ttl,
        }
        if execution_id is not None:
            item["execution_id"] = execution_id
        try:
            table = await self._table(self._chats_table)
            await table.put_item(Item=item)
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_PUT_CHAT_ID, session_id) from exc

    async def append_transcript(
        self, session_id: str, entry: TranscriptEntry, *, ttl: int
    ) -> None:
        """Persist one transcript entry to the transcripts table.

        One ``PutItem`` keyed by ``(session_id, entry.seq)`` (Req 2.5).
        Because the key is deterministic, a retried write of the same
        entry under the caller's bounded retry policy (Req 2.7) replaces
        the identical item — idempotent, never duplicated.

        Args:
            session_id: Identifier of the Voice_Session the entry
                belongs to.
            entry: The transcript line to persist, carrying its assigned
                monotonic ``seq``, role, text, and timestamp.
            ttl: Time-to-live attribute value in epoch seconds, computed
                by ``domain.ttl`` (Req 8.4).

        Raises:
            SessionStoreError: If the write fails; logged with the
                session identifier, and no partial record remains
                (Req 8.5).
        """
        item = _transcript_item(session_id, entry, ttl)
        try:
            table = await self._table(self._transcripts_table)
            await table.put_item(Item=item)
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_APPEND_TRANSCRIPT, session_id) from exc

    async def get_transcript(self, session_id: str) -> list[TranscriptEntry]:
        """Read all persisted transcript entries of a session, in order.

        A strongly consistent ``Query`` on the session's partition with
        ``ScanIndexForward=True``, so DynamoDB returns items ordered by
        the ascending numeric ``seq`` sort key (Req 8.3); the pagination
        loop follows ``LastEvaluatedKey`` so transcripts larger than one
        response page are read completely.

        Args:
            session_id: Identifier of the Voice_Session whose transcript
                to read.

        Returns:
            Every persisted entry for the session ordered by ascending
            ``seq``; an empty list when the session has no persisted
            entries.

        Raises:
            SessionStoreError: If the read fails; logged with the session
                identifier (Req 8.5).
        """
        items: list[Any] = []
        try:
            table = await self._table(self._transcripts_table)
            query_kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("session_id").eq(session_id),
                "ScanIndexForward": True,
                "ConsistentRead": True,
            }
            while True:
                response = await table.query(**query_kwargs)
                items.extend(response.get("Items", ()))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                query_kwargs["ExclusiveStartKey"] = last_key
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_GET_TRANSCRIPT, session_id) from exc
        return [_parse_transcript_entry(item) for item in items]

    async def put_subscription(self, subscription: WebPushSubscription) -> None:
        """Persist one Web_Push_Subscription registration.

        One ``PutItem`` upsert on the push-subscriptions table keyed by
        ``(engineer_id, endpoint_hash)``, so re-registering the same
        endpoint is idempotent (Req 6.1). The record carries no TTL
        (Req 6.5). The caller confirms the registration only after this
        returns (Req 8.7).

        Args:
            subscription: The subscription record to persist; its
                ``subscription`` mapping is stored as the item's ``M``
                attribute.

        Raises:
            SessionStoreError: If the write fails; the caller surfaces a
                registration error with a retry option (Req 6.7, 8.5).
        """
        item: dict[str, Any] = {
            "engineer_id": subscription.engineer_id,
            "endpoint_hash": subscription.endpoint_hash,
            "subscription": dict(subscription.subscription),
        }
        try:
            table = await self._table(self._subscriptions_table)
            await table.put_item(Item=item)
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_PUT_SUBSCRIPTION, None) from exc

    async def delete_subscription(
        self, engineer_id: str, endpoint_hash: str
    ) -> None:
        """Remove one Web_Push_Subscription record.

        One unconditional ``DeleteItem``: DynamoDB treats deleting an
        absent item as a success, so cleanup after a push-service
        rejection is idempotent (Req 6.5). The caller confirms the
        removal only after this returns (Req 8.7).

        Args:
            engineer_id: Cognito ``sub`` of the engineer who owns the
                subscription; the partition key.
            endpoint_hash: SHA-256 hex digest of the push endpoint URL;
                the sort key.

        Raises:
            SessionStoreError: If the delete fails (Req 8.5).
        """
        try:
            table = await self._table(self._subscriptions_table)
            await table.delete_item(
                Key={"engineer_id": engineer_id, "endpoint_hash": endpoint_hash}
            )
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_DELETE_SUBSCRIPTION, None) from exc

    async def list_subscriptions(self) -> list[WebPushSubscription]:
        """Read all registered Web_Push_Subscriptions.

        A strongly consistent full-table ``Scan`` — the designed access
        pattern for the Notifier's fan-out over a bounded population of
        on-call engineers (Req 6.2); the pagination loop follows
        ``LastEvaluatedKey`` so every page is read.

        Returns:
            Every registered subscription; an empty list when none are
            registered.

        Raises:
            SessionStoreError: If the read fails (Req 8.5).
        """
        items: list[Any] = []
        try:
            table = await self._table(self._subscriptions_table)
            scan_kwargs: dict[str, Any] = {"ConsistentRead": True}
            while True:
                response = await table.scan(**scan_kwargs)
                items.extend(response.get("Items", ()))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
        except _DYNAMODB_ERRORS as exc:
            raise _failure(_OP_LIST_SUBSCRIPTIONS, None) from exc
        return [_parse_subscription(item) for item in items]
