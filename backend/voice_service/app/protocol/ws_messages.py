"""WebSocket control-frame schemas for the Frontend ↔ Voice_Service protocol.

Pure protocol module implementing the design's "WebSocket Protocol
(Frontend ↔ Voice_Service)" section: typed frame dataclasses plus the
(de)serialization between them and the JSON text frames carried on the
``/ws/voice`` WebSocket. Binary frames (raw PCM audio) never pass through
this module.

Client → Server text frames (parsed by :func:`parse_client_frame`):

- ``session.start`` — opens a session, optionally scoped to a DevOps
  Agent ``executionId`` or carrying ``incidentContext`` summary/severity
  for incident-scoped sessions, or resuming an interrupted session via
  ``resumeSessionId`` (Req 8.6).
- ``session.end`` — graceful session end requested by the engineer.
- ``text.input`` — one typed engineer request, entering the conversation
  as a USER text block on the live stream exactly as a spoken turn does
  (identifiers such as instance ids are easier typed than pronounced).

Server → Client text frames (serialized by :func:`serialize_server_frame`):

- ``session.state`` — drives the frontend status badge with the five
  lowercase states ``connecting|live|segmenting|ended|error`` (Req 9.4).
- ``transcript`` — one engineer utterance or Nova_Sonic response line;
  the ``role`` value is lowercase ``user|assistant`` on the wire so the
  frontend can visually distinguish speakers (Req 1.4); the uppercase
  domain :class:`~app.domain.transcript.Role` is mapped on serialization.
- ``error`` — structured failure report with one of the six server
  categories (:class:`ErrorCategory`): ``bedrock_unavailable`` precedes
  close when a Bedrock_Stream cannot be opened (Req 1.6),
  ``session_not_found`` answers an unknown or expired ``resumeSessionId``
  (Req 8.6), ``auth_invalid``/``auth_expired`` report token failures,
  ``segmentation_failed`` reports a failed rollover, and ``internal``
  covers unexpected server errors.
- ``interrupted`` — barge-in notice: Nova_Sonic detected the engineer
  speaking over the assistant's response, so the client must flush its
  playback queue immediately (already-relayed audio for the cancelled
  response would otherwise keep playing over the new turn).
- ``session.terminating`` — sent to sessions still active when the drain
  period expires, before the WebSocket closes (Req 10.8).

Validation is lenient on extra keys (unknown keys anywhere are ignored,
mirroring the frontend's tolerance of unknown frame types) and strict on
the types of known keys: malformed JSON, a payload that is not a JSON
object, an unrecognized ``type``, or a wrongly typed known field raises
:class:`ProtocolError`.

This module performs no I/O and imports no SDKs; it depends only on the
pure domain vocabulary (``SessionState``, ``Role``, ``IncidentContext``)
so the session manager can hand domain values straight to the wire
(Req 17.6).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final, assert_never

from shared.exceptions import PortalError

from app.domain.session import IncidentContext, SessionState
from app.domain.transcript import Role

__all__ = [
    "ERROR_TYPE",
    "INTERRUPTED_TYPE",
    "SESSION_END_TYPE",
    "SESSION_START_TYPE",
    "SESSION_STATE_TYPE",
    "SESSION_TERMINATING_TYPE",
    "TEXT_INPUT_TYPE",
    "TRANSCRIPT_TYPE",
    "ClientFrame",
    "ErrorCategory",
    "ErrorFrame",
    "InterruptedFrame",
    "ProtocolError",
    "ServerFrame",
    "SessionEndFrame",
    "SessionStartFrame",
    "SessionStateFrame",
    "SessionTerminatingFrame",
    "TextInputFrame",
    "TranscriptFrame",
    "parse_client_frame",
    "serialize_server_frame",
]

SESSION_START_TYPE: Final = "session.start"
"""Wire ``type`` of the client frame that opens or resumes a session."""

SESSION_END_TYPE: Final = "session.end"
"""Wire ``type`` of the client frame that gracefully ends the session."""

TEXT_INPUT_TYPE: Final = "text.input"
"""Wire ``type`` of the client frame carrying one typed engineer request."""

SESSION_STATE_TYPE: Final = "session.state"
"""Wire ``type`` of the server frame driving the status badge (Req 9.4)."""

TRANSCRIPT_TYPE: Final = "transcript"
"""Wire ``type`` of the server frame carrying one transcript line (Req 1.4)."""

ERROR_TYPE: Final = "error"
"""Wire ``type`` of the server frame reporting a structured failure."""

INTERRUPTED_TYPE: Final = "interrupted"
"""Wire ``type`` of the server barge-in notice flushing client playback."""

SESSION_TERMINATING_TYPE: Final = "session.terminating"
"""Wire ``type`` of the server drain notice (Req 10.8)."""


class ProtocolError(PortalError):
    """A client text frame violates the WebSocket control-frame protocol.

    Raised by :func:`parse_client_frame` for malformed JSON, payloads
    that are not JSON objects, unrecognized frame types, and known fields
    carrying values of the wrong type. Extends the shared ``PortalError``
    hierarchy (Req 17.4) and is defined locally because it guards the
    wire contract owned by this protocol module; the connection handler
    maps it to an ``error`` frame or close.

    Attributes:
        problem: Short problem token: ``malformed-json``,
            ``not-an-object``, ``unknown-type``, or ``wrong-field-type``.
        field: Wire name of the offending field, when applicable.
        expected: Short token naming the expected value shape, when
            applicable.
        frame_type: The unrecognized frame ``type`` value, when the
            payload carried one as a string.
    """

    def __init__(
        self,
        problem: str,
        *,
        field: str | None = None,
        expected: str | None = None,
        frame_type: str | None = None,
    ) -> None:
        """Initialize the error with its typed diagnostic context.

        Args:
            problem: Short problem token describing the violation.
            field: Wire name of the offending field, when applicable.
            expected: Short token naming the expected value shape, when
                applicable.
            frame_type: The unrecognized frame ``type`` value, when the
                payload carried one as a string.
        """
        self.problem = problem
        self.field = field
        self.expected = expected
        self.frame_type = frame_type
        details = [
            part
            for part in (
                f"frame type {frame_type!r}" if frame_type is not None else None,
                f"field {field!r}" if field is not None else None,
                f"expected {expected}" if expected is not None else None,
            )
            if part is not None
        ]
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"Invalid WebSocket control frame: {problem}{suffix}")


@unique
class ErrorCategory(StrEnum):
    """Category carried by a server ``error`` frame.

    The member values are the exact lowercase wire strings of the design
    protocol. These eight are the only categories the server emits; the
    frontend additionally synthesizes client-local categories (such as
    ``connection_interrupted``) that never travel on this wire.

    Not every member reports a failure: ``IDLE_WARNING`` is an advisory
    notice sent mid-session with ``recoverable=True``. It rides the
    ``error`` frame rather than a new frame type because that frame
    already carries the ``category``/``message``/``recoverable`` triple
    the frontend's alert catalog renders from, and ``recoverable`` is
    exactly the flag that separates a notice the session survives from a
    terminal one.
    """

    AUTH_INVALID = "auth_invalid"
    """The presented JWT failed validation (maps from ``TokenInvalidError``)."""

    AUTH_EXPIRED = "auth_expired"
    """The JWT expired, including mid-session expiry before close (Req 7.6)."""

    BEDROCK_UNAVAILABLE = "bedrock_unavailable"
    """A Bedrock_Stream could not be opened; precedes close (Req 1.6)."""

    SEGMENTATION_FAILED = "segmentation_failed"
    """A segmentation rollover failed or overran its watchdog (Req 2.6)."""

    SESSION_NOT_FOUND = "session_not_found"
    """A ``resumeSessionId`` names a missing or expired session (Req 8.6)."""

    IDLE_WARNING = "idle_warning"
    """No engineer turn for the idle warning period; the session still runs.

    Advisory, sent with ``recoverable=True``: speaking or typing clears
    the idle interval and the session continues normally.
    """

    IDLE_TIMEOUT = "idle_timeout"
    """No engineer turn through the warning and its grace period; precedes close."""

    INTERNAL = "internal"
    """An unexpected server-side error not covered by another category."""


@dataclass(frozen=True, slots=True)
class SessionStartFrame:
    """Client request to open a Voice_Session (wire type ``session.start``).

    Sent as the first text frame after the WebSocket upgrade. At most one
    scoping field is typically present: ``execution_id`` for a session
    opened from an incident carrying a DevOps Agent execution,
    ``incident_context`` for one opened from an incident without it, and
    ``resume_session_id`` when reconnecting to an interrupted session
    (Req 8.6); a plain Start-button session carries all three as ``None``.

    Attributes:
        execution_id: DevOps Agent execution scope (wire ``executionId``),
            or ``None``.
        incident_context: Incident summary/severity context (wire
            ``incidentContext``), or ``None``.
        resume_session_id: Voice_Session id to resume (wire
            ``resumeSessionId``), or ``None``.
    """

    execution_id: str | None = None
    incident_context: IncidentContext | None = None
    resume_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionEndFrame:
    """Client request to end the session gracefully (wire type ``session.end``).

    Carries no fields; the session manager responds by draining the
    Bedrock_Stream (promptEnd/sessionEnd), persisting the final
    transcript, and reporting the ``ended`` state.
    """


@dataclass(frozen=True, slots=True)
class TextInputFrame:
    """One typed engineer request (wire type ``text.input``).

    Typing complements speaking rather than replacing it: resource
    identifiers, ARNs, and error strings are far easier to type than to
    pronounce, so the engineer may send them mid-session while the voice
    stream stays open. The session manager forwards the text to the live
    Bedrock_Stream as a USER text block, so Nova_Sonic answers it by voice
    exactly as it answers a spoken turn, and persists it as a transcript
    entry (Req 1.4, 8.2).

    Attributes:
        text: The typed request text, exactly as entered; never blank
            (a blank ``text`` fails parsing).
    """

    text: str


type ClientFrame = SessionStartFrame | SessionEndFrame | TextInputFrame
"""Union of the three client → server control frames."""


@dataclass(frozen=True, slots=True)
class SessionStateFrame:
    """Server announcement of the session state (wire type ``session.state``).

    Drives the frontend status badge through its five states (Req 9.4);
    the session manager emits one only after the matching Session_Store
    write completed (Req 8.2).

    Attributes:
        session_id: Voice_Session identifier (wire ``sessionId``); the
            frontend stores it as the resume target for reconnects.
        state: Domain session state; serialized as its lowercase
            :attr:`~app.domain.session.SessionState.ws_state` value.
    """

    session_id: str
    state: SessionState


@dataclass(frozen=True, slots=True)
class TranscriptFrame:
    """Server relay of one transcript line (wire type ``transcript``).

    Attributes:
        role: Domain speaker role (``USER``/``ASSISTANT``); serialized as
            the lowercase wire values ``user``/``assistant`` so the
            frontend can visually distinguish speakers (Req 1.4).
        text: Utterance or response text.
        timestamp: ISO-8601 UTC time of the entry, injected by the
            caller's clock.
    """

    role: Role
    text: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    """Server report of a structured failure (wire type ``error``).

    Attributes:
        category: One of the six server failure categories.
        message: Human-readable failure description; never carries secret
            values.
        recoverable: Whether the engineer can retry within the same
            session context (for example by reconnecting).
    """

    category: ErrorCategory
    message: str
    recoverable: bool


@dataclass(frozen=True, slots=True)
class InterruptedFrame:
    """Server barge-in notice (wire type ``interrupted``).

    Sent when Nova_Sonic reports that the engineer interrupted the spoken
    response (a ``contentEnd`` with ``stopReason`` ``INTERRUPTED`` or the
    ``textOutput`` interruption marker). The client reacts by flushing its
    playback queue so the cancelled response stops immediately instead of
    playing over the engineer's new turn. Carries no fields.
    """


@dataclass(frozen=True, slots=True)
class SessionTerminatingFrame:
    """Server drain notice preceding a close (wire type ``session.terminating``).

    Sent to sessions still active when the drain period expires so the
    client can offer a reconnect onto a healthy task (Req 10.8).

    Attributes:
        reason: Short machine-oriented reason token, for example
            ``drain_timeout``.
    """

    reason: str


type ServerFrame = (
    SessionStateFrame
    | TranscriptFrame
    | ErrorFrame
    | InterruptedFrame
    | SessionTerminatingFrame
)
"""Union of the five server → client control frames."""


def parse_client_frame(text: str) -> ClientFrame:
    """Parse one client text frame into its typed representation.

    Args:
        text: Raw text payload of a WebSocket frame received from the
            Frontend.

    Returns:
        A :class:`SessionStartFrame` for ``session.start`` frames (absent
        optional keys parse as ``None``) or a :class:`SessionEndFrame`
        for ``session.end`` frames. Unknown keys anywhere in the payload
        are ignored.

    Raises:
        ProtocolError: If ``text`` is not valid JSON, the payload is not
            a JSON object, the ``type`` value is missing or not a
            recognized frame type, or a known field carries a value of
            the wrong type.
    """
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProtocolError("malformed-json") from error
    payload = _as_object(decoded)
    frame_type = payload.get("type")
    if frame_type == SESSION_START_TYPE:
        return _parse_session_start(payload)
    if frame_type == SESSION_END_TYPE:
        return SessionEndFrame()
    if frame_type == TEXT_INPUT_TYPE:
        return _parse_text_input(payload)
    observed = frame_type if isinstance(frame_type, str) else None
    raise ProtocolError("unknown-type", field="type", frame_type=observed)


def _parse_text_input(payload: Mapping[object, object]) -> TextInputFrame:
    """Extract the typed request of a ``text.input`` frame.

    Args:
        payload: Decoded JSON object whose ``type`` is ``text.input``.

    Returns:
        The typed frame carrying the request text.

    Raises:
        ProtocolError: If ``text`` is missing, not a string, or blank —
            a blank typed request carries no request at all, so it is
            rejected at the wire boundary rather than reaching the
            guardrail gate.
    """
    value = payload.get("text")
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(
            "wrong-field-type", field="text", expected="non-blank-string"
        )
    return TextInputFrame(text=value)


def serialize_server_frame(frame: ServerFrame) -> str:
    """Serialize one server frame to its exact wire JSON text.

    Args:
        frame: The typed server frame to send.

    Returns:
        Compact JSON carrying the wire field names and value vocabularies
        of the design protocol: ``session.state`` carries the lowercase
        state (Req 9.4), ``transcript`` carries the lowercase role
        (Req 1.4), ``error`` carries the :class:`ErrorCategory` value,
        and ``session.terminating`` carries the drain reason (Req 10.8).
    """
    payload: dict[str, object]
    match frame:
        case SessionStateFrame(session_id=session_id, state=state):
            payload = {
                "type": SESSION_STATE_TYPE,
                "sessionId": session_id,
                "state": state.ws_state,
            }
        case TranscriptFrame(role=role, text=text, timestamp=timestamp):
            payload = {
                "type": TRANSCRIPT_TYPE,
                "role": role.value.lower(),
                "text": text,
                "timestamp": timestamp,
            }
        case ErrorFrame(category=category, message=message, recoverable=recoverable):
            payload = {
                "type": ERROR_TYPE,
                "category": category.value,
                "message": message,
                "recoverable": recoverable,
            }
        case InterruptedFrame():
            payload = {"type": INTERRUPTED_TYPE}
        case SessionTerminatingFrame(reason=reason):
            payload = {
                "type": SESSION_TERMINATING_TYPE,
                "reason": reason,
            }
        case _:
            assert_never(frame)
    return json.dumps(payload, separators=(",", ":"))


def _as_object(decoded: object) -> Mapping[object, object]:
    """Return the decoded payload as a JSON-object mapping.

    Args:
        decoded: Value produced by ``json.loads``.

    Returns:
        The payload as a mapping when it is a JSON object.

    Raises:
        ProtocolError: If the decoded payload is not a JSON object.
    """
    if isinstance(decoded, dict):
        return decoded
    raise ProtocolError("not-an-object")


def _parse_session_start(payload: Mapping[object, object]) -> SessionStartFrame:
    """Extract the typed fields of a ``session.start`` frame.

    Args:
        payload: Decoded JSON object whose ``type`` is ``session.start``.

    Returns:
        The typed frame; absent optional keys parse as ``None`` and
        unknown keys are ignored.

    Raises:
        ProtocolError: If a known field carries a value of the wrong
            type.
    """
    return SessionStartFrame(
        execution_id=_string_or_null(payload, "executionId"),
        incident_context=_incident_context_or_null(payload),
        resume_session_id=_string_or_null(payload, "resumeSessionId"),
    )


def _string_or_null(payload: Mapping[object, object], key: str) -> str | None:
    """Read an optional string field, treating an absent key as null.

    Args:
        payload: Decoded JSON object to read from.
        key: Wire name of the field.

    Returns:
        The string value, or ``None`` when the key is absent or null.

    Raises:
        ProtocolError: If the value is neither a string nor null.
    """
    value = payload.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ProtocolError("wrong-field-type", field=key, expected="string-or-null")


def _incident_context_or_null(
    payload: Mapping[object, object],
) -> IncidentContext | None:
    """Read the optional ``incidentContext`` object of a ``session.start`` frame.

    Args:
        payload: Decoded JSON object of the ``session.start`` frame.

    Returns:
        The incident summary/severity context carried by incident-scoped
        sessions (Req 5.8), or ``None`` when the key is absent or null.

    Raises:
        ProtocolError: If the value is neither an object nor null, or its
            ``summary`` or ``severity`` member is missing or not a
            string.
    """
    value = payload.get("incidentContext")
    if value is None:
        return None
    if isinstance(value, dict):
        context: Mapping[object, object] = value
        return IncidentContext(
            summary=_context_string(context, "summary"),
            severity=_context_string(context, "severity"),
        )
    raise ProtocolError(
        "wrong-field-type", field="incidentContext", expected="object-or-null"
    )


def _context_string(context: Mapping[object, object], key: str) -> str:
    """Read a required string member of the ``incidentContext`` object.

    Args:
        context: Decoded ``incidentContext`` JSON object.
        key: Member name (``summary`` or ``severity``).

    Returns:
        The member's string value.

    Raises:
        ProtocolError: If the member is absent or not a string.
    """
    value = context.get(key)
    if isinstance(value, str):
        return value
    raise ProtocolError(
        "wrong-field-type", field=f"incidentContext.{key}", expected="string"
    )
