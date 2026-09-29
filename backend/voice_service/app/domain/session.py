"""Voice_Session state machine: states, legal transitions, status mappings.

Pure domain module implementing the design's session segmentation state
machine. A ``VoiceSession`` starts in ``CONNECTING`` and moves along these
edges only:

- ``CONNECTING → LIVE``: the first Bedrock_Stream opened.
- ``CONNECTING → ERROR``: the Bedrock_Stream could not be opened (Req 1.6).
- ``LIVE → SEGMENTING``: stream age reached 7 m 30 s (Req 2.2).
- ``SEGMENTING → LIVE``: segmentation rollover completed (Req 2.2).
- ``SEGMENTING → ERROR``: rollover failed or exceeded the 10-second
  watchdog (Req 2.6).
- ``LIVE → ENDED``: the engineer ended the session or the WebSocket
  closed (Req 2.5).

``ENDED`` and ``ERROR`` are terminal: no event may leave them.

Transitions are pure and immutable: :func:`transition` returns a new
``VoiceSession`` so the caller can persist the new record to the
Session_Store first and only then emit the matching ``session.state``
status frame (Req 8.2, 9.4). Two status vocabularies hang off the same
machine: the WebSocket protocol uses the lowercase enum values
(``connecting|live|segmenting|ended|error``, Req 9.4) while the
Session_Store persists ``CREATED|LIVE|SEGMENTING|ENDED|ERROR`` — note
``CONNECTING`` maps to the stored status ``CREATED``. The
:attr:`SessionState.ws_state` and :attr:`SessionState.store_status`
helpers provide both mappings.

Timestamps are injected by the caller: the domain never reads a clock,
performs no I/O, and imports no SDKs (Req 17.6).

``IllegalTransitionError`` is defined here rather than in
``app.exceptions``: it extends the shared ``PortalError`` hierarchy
(Req 17.4) but guards a purely domain-internal invariant of this state
machine, so it lives next to the machine it protects.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Final

from shared.exceptions import PortalError

__all__ = [
    "IllegalTransitionError",
    "IncidentContext",
    "SessionEvent",
    "SessionState",
    "VoiceSession",
    "create_session",
    "transition",
]


@unique
class SessionState(StrEnum):
    """State of a Voice_Session in the segmentation state machine.

    The enum values are the exact lowercase strings carried by the
    WebSocket ``session.state`` frame that drives the frontend status
    badge (Req 9.4).
    """

    CONNECTING = "connecting"
    LIVE = "live"
    SEGMENTING = "segmenting"
    ENDED = "ended"
    ERROR = "error"

    @property
    def ws_state(self) -> str:
        """Return the WebSocket protocol representation of this state.

        Returns:
            The lowercase ``state`` value used in ``session.state``
            frames: one of ``connecting``, ``live``, ``segmenting``,
            ``ended``, or ``error`` (Req 9.4).
        """
        return self.value

    @property
    def store_status(self) -> str:
        """Return the Session_Store ``status`` attribute for this state.

        The voice-sessions table persists ``CREATED|LIVE|SEGMENTING|
        ENDED|ERROR``; a session that is still connecting is stored as
        ``CREATED``.

        Returns:
            The uppercase DynamoDB ``status`` value: ``CREATED`` for
            ``CONNECTING``, otherwise the state name.
        """
        return "CREATED" if self is SessionState.CONNECTING else self.name

    @property
    def is_terminal(self) -> bool:
        """Return whether this state admits no further transitions.

        Returns:
            ``True`` for ``ENDED`` and ``ERROR``, ``False`` otherwise.
        """
        return self in {SessionState.ENDED, SessionState.ERROR}


@unique
class SessionEvent(StrEnum):
    """Event that drives a Voice_Session state transition.

    Each member names the design state-machine edge trigger it
    represents; orchestration raises these events and consumes the
    resulting session for persistence and status frames.
    """

    STREAM_OPENED = "stream_opened"
    """The Bedrock_Stream opened while connecting (``CONNECTING → LIVE``)."""

    STREAM_OPEN_FAILED = "stream_open_failed"
    """The Bedrock_Stream could not be opened (``CONNECTING → ERROR``, Req 1.6)."""

    SEGMENTATION_DUE = "segmentation_due"
    """Stream age reached 7 m 30 s (``LIVE → SEGMENTING``, Req 2.2)."""

    ROLLOVER_COMPLETE = "rollover_complete"
    """Segmentation rollover completed (``SEGMENTING → LIVE``)."""

    SEGMENTATION_FAILED = "segmentation_failed"
    """Rollover failed or exceeded the 10 s watchdog (``SEGMENTING → ERROR``, Req 2.6)."""

    SESSION_ENDED = "session_ended"
    """The engineer ended the session or the WebSocket closed (``LIVE → ENDED``, Req 2.5)."""


_TRANSITIONS: Final[Mapping[tuple[SessionState, SessionEvent], SessionState]] = (
    MappingProxyType(
        {
            (SessionState.CONNECTING, SessionEvent.STREAM_OPENED): SessionState.LIVE,
            (SessionState.CONNECTING, SessionEvent.STREAM_OPEN_FAILED): SessionState.ERROR,
            (SessionState.LIVE, SessionEvent.SEGMENTATION_DUE): SessionState.SEGMENTING,
            (SessionState.LIVE, SessionEvent.SESSION_ENDED): SessionState.ENDED,
            (SessionState.SEGMENTING, SessionEvent.ROLLOVER_COMPLETE): SessionState.LIVE,
            (SessionState.SEGMENTING, SessionEvent.SEGMENTATION_FAILED): SessionState.ERROR,
        }
    )
)
"""The legal state-machine edges: ``(current state, event) -> next state``."""


class IllegalTransitionError(PortalError):
    """A Voice_Session event was raised in a state that forbids it.

    Raised by :func:`transition` when the ``(state, event)`` pair is not
    a legal edge of the design state machine — for example any event on
    the terminal ``ENDED`` and ``ERROR`` states. This class extends the
    shared ``PortalError`` hierarchy (Req 17.4) and is defined locally
    because it guards an invariant internal to this domain module.

    Attributes:
        state: The session state the illegal event was raised in.
        event: The event that no legal edge accepts from ``state``.
    """

    def __init__(self, state: SessionState, event: SessionEvent) -> None:
        """Initialize the error with the offending state and event.

        Args:
            state: The session state the illegal event was raised in.
            event: The event that no legal edge accepts from ``state``.
        """
        self.state = state
        self.event = event
        super().__init__(
            f"Illegal Voice_Session transition: event {event.value!r} "
            f"in state {state.value!r}"
        )


@dataclass(frozen=True, slots=True)
class IncidentContext:
    """Incident summary carried by a session opened without an executionId.

    When an incident notification carries no ``executionId``, the popup
    click opens a Voice_Session that carries the incident summary and
    severity as context instead (Req 5.8); the pair is persisted in the
    voice-sessions table ``incident_context`` map attribute.

    Attributes:
        summary: Human-readable incident summary.
        severity: Incident severity label.
    """

    summary: str
    severity: str


@dataclass(frozen=True, slots=True)
class VoiceSession:
    """Immutable snapshot of a Voice_Session, one per state-machine step.

    Mirrors the voice-sessions table record (Req 8.1): consumers persist
    a snapshot via :attr:`SessionState.store_status` and report it via
    :attr:`SessionState.ws_state`. Instances never mutate; use
    :func:`transition` to derive the next snapshot.

    Attributes:
        session_id: UUIDv4 identifier of the Voice_Session.
        engineer_id: Cognito ``sub`` of the engineer who owns the session.
        created_at: ISO-8601 UTC creation timestamp, injected by the caller.
        updated_at: ISO-8601 UTC timestamp of the latest transition,
            injected by the caller.
        state: Current state-machine state.
        execution_id: DevOps Agent execution scope when the session was
            opened from an incident that carried one (Req 3.5); ``None``
            otherwise.
        incident_context: Incident summary/severity context when the
            session was opened from an incident without an executionId
            (Req 5.8); ``None`` otherwise.
        segment_count: Number of Bedrock_Streams that have gone live for
            this session so far: 0 while connecting, 1 once live, and one
            more for every completed segmentation rollover.
    """

    session_id: str
    engineer_id: str
    created_at: str
    updated_at: str
    state: SessionState = SessionState.CONNECTING
    execution_id: str | None = None
    incident_context: IncidentContext | None = None
    segment_count: int = 0


def create_session(
    *,
    session_id: str,
    engineer_id: str,
    now: str,
    execution_id: str | None = None,
    incident_context: IncidentContext | None = None,
) -> VoiceSession:
    """Create a new Voice_Session in the initial ``CONNECTING`` state.

    The timestamp is injected so this module never reads a clock: both
    ``created_at`` and ``updated_at`` are set to ``now``, and
    ``segment_count`` starts at 0 because no Bedrock_Stream is live yet.

    Args:
        session_id: UUIDv4 identifier of the new Voice_Session.
        engineer_id: Cognito ``sub`` of the engineer opening the session.
        now: Current time as an ISO-8601 UTC string, supplied by the
            caller's clock.
        execution_id: DevOps Agent execution scope when the session is
            opened from an incident that carries one (Req 3.5).
        incident_context: Incident summary/severity context when the
            session is opened from an incident without an executionId
            (Req 5.8).

    Returns:
        A ``VoiceSession`` in state ``CONNECTING`` with both timestamps
        set to ``now``.
    """
    return VoiceSession(
        session_id=session_id,
        engineer_id=engineer_id,
        created_at=now,
        updated_at=now,
        execution_id=execution_id,
        incident_context=incident_context,
    )


def transition(session: VoiceSession, event: SessionEvent, *, now: str) -> VoiceSession:
    """Advance a Voice_Session along one legal state-machine edge.

    Looks up the ``(session.state, event)`` edge in the design state
    machine and returns a new snapshot in the target state with
    ``updated_at`` set to ``now``. ``segment_count`` increments on every
    edge entering ``LIVE`` — the first stream on ``CONNECTING → LIVE``
    and each replacement stream on a ``SEGMENTING → LIVE`` rollover
    completion. The input snapshot is never mutated, so the caller can
    persist the returned snapshot to the Session_Store before reporting
    the state change in a status frame (Req 8.2, 9.4).

    Args:
        session: Current Voice_Session snapshot.
        event: Event driving the transition.
        now: Current time as an ISO-8601 UTC string, supplied by the
            caller's clock; becomes the new ``updated_at``.

    Returns:
        A new ``VoiceSession`` snapshot in the transition's target state.

    Raises:
        IllegalTransitionError: If the state machine has no edge for
            ``(session.state, event)``, including any event raised on the
            terminal ``ENDED`` and ``ERROR`` states.
    """
    next_state = _TRANSITIONS.get((session.state, event))
    if next_state is None:
        raise IllegalTransitionError(session.state, event)
    segment_count = session.segment_count + (1 if next_state is SessionState.LIVE else 0)
    return replace(session, state=next_state, segment_count=segment_count, updated_at=now)
