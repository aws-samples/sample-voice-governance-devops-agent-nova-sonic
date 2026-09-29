"""Abstract interface to the DynamoDB-backed Session_Store (Req 8.1).

``SessionStorePort`` is the port boundary (Req 17.6) between
orchestration and the Session_Store. Its method groups mirror the four
tables of the design's data model one-to-one:

- **voice-sessions** (Voice_Session state, Req 8.1, 8.2):
  :meth:`SessionStorePort.get_session` /
  :meth:`SessionStorePort.put_session`.
- **agent-chats** (DevOps_Agent chat mapping, Req 3.2):
  :meth:`SessionStorePort.get_chat_id` /
  :meth:`SessionStorePort.put_chat_id`.
- **transcripts** (conversation transcripts, Req 2.5, 8.3):
  :meth:`SessionStorePort.append_transcript` /
  :meth:`SessionStorePort.get_transcript`.
- **push-subscriptions** (Web_Push_Subscriptions, Req 6.1, 8.7):
  :meth:`SessionStorePort.put_subscription` /
  :meth:`SessionStorePort.delete_subscription` /
  :meth:`SessionStorePort.list_subscriptions`.

``ttl`` arguments are DynamoDB time-to-live attribute values in epoch
seconds, computed by ``domain.ttl`` (last update time plus the
configured retention period, Req 8.4). Subscription records carry no
TTL — they are removed explicitly on unsubscribe or push-service
rejection (Req 6.5).

Write discipline (Req 8.5): implementations perform each mutation as a
conditional single-item write (or a transactional write where two items
must move together), so a failure leaves no partially updated record.
Every failed read or write raises ``SessionStoreError``, logged with the
affected Voice_Session identifier where the operation is
session-scoped. Persist-before-confirm ordering (Req 8.2, 8.7) is the
caller's responsibility; the port's guarantee is that each coroutine
returns only after its write is durable.

The implementing adapter (``adapters.dynamodb_store``) owns all aioboto3
access; test suites substitute the deterministic in-memory
``FakeSessionStore``. This module imports no SDK (Req 17.6).
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.session import VoiceSession
from app.domain.transcript import TranscriptEntry

__all__ = ["SessionStorePort", "WebPushSubscription"]


@dataclass(frozen=True, slots=True)
class WebPushSubscription:
    """Immutable Web_Push_Subscription, mirroring one table item (Req 6.1).

    Mirrors the push-subscriptions table shape: keyed by
    ``(engineer_id, endpoint_hash)`` and carrying the browser
    subscription object the Notifier hands to the Web Push API. Treat
    the ``subscription`` mapping as deeply immutable.

    Attributes:
        engineer_id: Cognito ``sub`` of the engineer who registered the
            subscription; the table's partition key.
        endpoint_hash: SHA-256 hex digest of the push endpoint URL; the
            table's sort key, making registrations idempotent per
            endpoint.
        subscription: The browser push subscription as a JSON-ready
            mapping in the shape
            ``{"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}``.
    """

    engineer_id: str
    endpoint_hash: str
    subscription: Mapping[str, object]


class SessionStorePort(ABC):
    """The Session_Store, as orchestration and the Notifier see it.

    Abstract base class over the four Session_Store tables (Req 8.1).
    Methods are grouped per table; all of them raise
    ``SessionStoreError`` on failure and leave no partial record behind
    (Req 8.5).
    """

    @abstractmethod
    async def get_session(self, session_id: str) -> VoiceSession | None:
        """Read one Voice_Session record.

        Backs reconnect restoration: the caller restores the persisted
        session state (with its transcripts) into a fresh stream
        (Req 8.3), and treats an absent record as a rejected
        reconnection with a ``session_not_found`` error (Req 8.6).

        Args:
            session_id: Identifier of the Voice_Session to read.

        Returns:
            The persisted ``VoiceSession`` snapshot, or ``None`` when no
            record exists for ``session_id`` — never created, expired by
            TTL, or already removed (Req 8.6).

        Raises:
            SessionStoreError: If the read fails (Req 8.5).
        """

    @abstractmethod
    async def put_session(self, session: VoiceSession, *, ttl: int) -> None:
        """Persist one Voice_Session snapshot.

        Upserts the record for ``session.session_id`` with the
        snapshot's current state. The caller invokes this on every state
        transition and reports the state change (status frame) only
        after this coroutine returns (Req 8.2).

        Args:
            session: The Voice_Session snapshot to persist, stored via
                its ``state.store_status`` representation.
            ttl: Time-to-live attribute value in epoch seconds, computed
                by ``domain.ttl`` from the snapshot's ``updated_at``
                (Req 8.4).

        Raises:
            SessionStoreError: If the write fails; no partially updated
                record remains (Req 8.5).
        """

    @abstractmethod
    async def get_chat_id(self, session_id: str) -> str | None:
        """Read the DevOps_Agent chat identifier mapped to a session.

        Args:
            session_id: Identifier of the Voice_Session whose chat
                mapping to read.

        Returns:
            The persisted chat identifier to reuse for subsequent agent
            requests (Req 3.9), or ``None`` when no mapping exists — the
            caller then creates a new chat and persists it (Req 3.10).

        Raises:
            SessionStoreError: If the read fails (Req 8.5).
        """

    @abstractmethod
    async def put_chat_id(
        self,
        session_id: str,
        chat_id: str,
        *,
        ttl: int,
        execution_id: str | None = None,
    ) -> None:
        """Persist the DevOps_Agent chat mapping for a session.

        Records the chat created by ``DevOpsAgentPort.create_chat`` so
        every subsequent request in the Voice_Session reuses it
        (Req 3.2, 3.9).

        Args:
            session_id: Identifier of the owning Voice_Session; the
                mapping's key (one chat per session).
            chat_id: The chat identifier returned by
                ``aidevops:CreateChat``.
            ttl: Time-to-live attribute value in epoch seconds, aligned
                with the owning session's TTL (Req 8.4).
            execution_id: The DevOps_Agent execution the chat was scoped
                to at creation, when the session carries one (Req 3.5);
                ``None`` for an unscoped chat (Req 3.6).

        Raises:
            SessionStoreError: If the write fails; no partially updated
                record remains (Req 8.5).
        """

    @abstractmethod
    async def append_transcript(
        self, session_id: str, entry: TranscriptEntry, *, ttl: int
    ) -> None:
        """Persist one transcript entry for a session.

        Writes the entry as one transcripts-table item keyed by
        ``(session_id, entry.seq)`` (Req 2.5). The caller retries failed
        writes under the shared bounded retry policy (Req 2.7).

        Args:
            session_id: Identifier of the Voice_Session the entry
                belongs to.
            entry: The transcript line to persist, carrying its assigned
                monotonic ``seq``, role, text, and timestamp.
            ttl: Time-to-live attribute value in epoch seconds, computed
                by ``domain.ttl`` (Req 8.4).

        Raises:
            SessionStoreError: If the write fails; no partially updated
                record remains (Req 8.5).
        """

    @abstractmethod
    async def get_transcript(self, session_id: str) -> list[TranscriptEntry]:
        """Read all persisted transcript entries of a session.

        Backs reconnect restoration: the returned entries are fed to
        ``TranscriptAccumulator.restore`` and replayed into a fresh
        stream (Req 8.3).

        Args:
            session_id: Identifier of the Voice_Session whose transcript
                to read.

        Returns:
            Every persisted entry for the session ordered by ascending
            ``seq``; an empty list when the session has no persisted
            entries.

        Raises:
            SessionStoreError: If the read fails (Req 8.5).
        """

    @abstractmethod
    async def put_subscription(self, subscription: WebPushSubscription) -> None:
        """Persist one Web_Push_Subscription registration.

        Upserts the record keyed by
        ``(engineer_id, endpoint_hash)``; re-registering the same
        endpoint is idempotent. The caller confirms the registration to
        the browser only after this coroutine returns (Req 6.1, 8.7).

        Args:
            subscription: The subscription record to persist.

        Raises:
            SessionStoreError: If the write fails; the caller surfaces a
                registration error with a retry option (Req 6.7, 8.5).
        """

    @abstractmethod
    async def delete_subscription(
        self, engineer_id: str, endpoint_hash: str
    ) -> None:
        """Remove one Web_Push_Subscription.

        Used on explicit unsubscribe and by the Notifier when the push
        service rejects the subscription as expired or invalid
        (404/410), after which no further deliveries are attempted
        (Req 6.5). Deleting an absent record is a no-op, so cleanup is
        idempotent. The caller confirms the removal only after this
        coroutine returns (Req 8.7).

        Args:
            engineer_id: Cognito ``sub`` of the engineer who owns the
                subscription; the partition key.
            endpoint_hash: SHA-256 hex digest of the push endpoint URL;
                the sort key.

        Raises:
            SessionStoreError: If the delete fails (Req 8.5).
        """

    @abstractmethod
    async def list_subscriptions(self) -> list[WebPushSubscription]:
        """Read all registered Web_Push_Subscriptions.

        Backs the Notifier's push fan-out, which delivers each
        Incident_Notification to every registered subscription
        (Req 6.2). The population is bounded (engineers on call), so a
        full read is the designed access pattern.

        Returns:
            Every registered subscription; an empty list when none are
            registered.

        Raises:
            SessionStoreError: If the read fails (Req 8.5).
        """
