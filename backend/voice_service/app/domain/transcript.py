"""Transcript accumulation: ordered role/text entries and streamed-chunk joining.

Pure domain module backing three consumers:

- The WebSocket ``transcript`` frames, which carry a role so the frontend
  can visually distinguish engineer utterances from Nova_Sonic responses
  (Req 1.4).
- The transcript persister, which writes the accumulated entries to the
  Session_Store transcripts table — one item per entry, keyed by
  ``(session_id, seq)`` with ``seq`` monotonically increasing per session
  — and persists the final transcript when the Voice_Session ends
  (Req 2.5).
- The tool router, which accumulates the DevOps_Agent's streamed response
  chunks into one complete response text before returning the tool result
  (Req 3.3, design Property 4).

Sequence numbering starts at :data:`FIRST_SEQ` (0): the first appended
entry receives ``seq`` 0 and every subsequent entry receives the previous
``seq`` plus one, so ``seq`` is strictly monotonically increasing and, for
a session that never restored, equals the entry's position. On reconnect
(Req 8.3) :meth:`TranscriptAccumulator.restore` reloads the persisted
entries and continues numbering from the highest restored ``seq`` plus
one, so restored and new entries never collide.

The segmentation replay builder consumes
:meth:`TranscriptAccumulator.entries` to rebuild conversation history in
original chronological order.

Timestamps are injected by the caller as ISO-8601 UTC strings: this
module never reads a clock, performs no I/O, and imports no SDKs
(Req 17.6).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final

__all__ = [
    "FIRST_SEQ",
    "ChunkAccumulator",
    "Role",
    "TranscriptAccumulator",
    "TranscriptEntry",
]

FIRST_SEQ: Final = 0
"""Sequence number assigned to the first entry of a fresh accumulator."""


@unique
class Role(StrEnum):
    """Speaker role of a transcript entry.

    The member values are the exact uppercase strings persisted in the
    transcripts table ``role`` attribute (``USER|ASSISTANT``) and carried
    in ``transcript`` frames so the frontend can visually distinguish
    engineer utterances from Nova_Sonic responses (Req 1.4).
    """

    USER = "USER"
    """An engineer utterance (ASR text from Nova_Sonic ``textOutput``)."""

    ASSISTANT = "ASSISTANT"
    """A Nova_Sonic spoken-response transcript."""


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """Immutable transcript line, mirroring one transcripts-table item.

    Attributes:
        seq: Position of the entry within its session; strictly
            monotonically increasing per session, starting at
            :data:`FIRST_SEQ`. Persisted as the table's numeric sort key.
        role: Speaker of the entry (``USER`` or ``ASSISTANT``).
        text: Utterance or response text.
        timestamp: ISO-8601 UTC time of the entry, injected by the
            caller's clock.
    """

    seq: int
    role: Role
    text: str
    timestamp: str


class TranscriptAccumulator:
    """Ordered per-session accumulation of transcript entries.

    Entries are held in append order and returned as an immutable
    snapshot by :meth:`entries`; each append assigns the next sequence
    number, so ``seq`` is strictly monotonically increasing across the
    accumulator's lifetime (Req 2.5). :meth:`restore` reloads entries
    persisted in the Session_Store so a reconnected session continues
    numbering where it left off (Req 8.3).
    """

    def __init__(self) -> None:
        """Initialize an empty accumulator; the next ``seq`` is :data:`FIRST_SEQ`."""
        self._entries: list[TranscriptEntry] = []
        self._next_seq = FIRST_SEQ

    def append(self, role: Role, text: str, timestamp: str) -> TranscriptEntry:
        """Record a transcript line and assign it the next sequence number.

        Args:
            role: Speaker of the line (``USER`` or ``ASSISTANT``).
            text: Utterance or response text.
            timestamp: ISO-8601 UTC time of the line, supplied by the
                caller's clock.

        Returns:
            The stored ``TranscriptEntry`` carrying the assigned ``seq``,
            ready to be persisted to the Session_Store and relayed as a
            ``transcript`` frame (Req 1.4, 2.5).
        """
        entry = TranscriptEntry(
            seq=self._next_seq, role=role, text=text, timestamp=timestamp
        )
        self._entries.append(entry)
        self._next_seq += 1
        return entry

    def entries(self) -> tuple[TranscriptEntry, ...]:
        """Return an immutable snapshot of all entries in ``seq`` order.

        The segmentation replay builder consumes this snapshot to rebuild
        conversation history in original chronological order, and the
        persister uses it to write the final transcript when the session
        ends (Req 2.5).

        Returns:
            All accumulated entries ordered by ascending ``seq``; empty
            when nothing has been appended or restored.
        """
        return tuple(self._entries)

    def restore(self, entries: Iterable[TranscriptEntry]) -> None:
        """Reload persisted entries, replacing the accumulator's contents.

        Used on reconnect (Req 8.3): the entries read back from the
        Session_Store are re-ordered by ``seq`` (the store returns them
        sorted by the sort key, but ordering here keeps the
        :meth:`entries` invariant unconditional) and become the
        accumulator's contents. Subsequent appends continue numbering at
        the highest restored ``seq`` plus one; restoring nothing resets
        numbering to :data:`FIRST_SEQ`.

        Args:
            entries: Previously persisted entries for the session being
                resumed, in any order.
        """
        restored = sorted(entries, key=lambda entry: entry.seq)
        self._entries = restored
        self._next_seq = restored[-1].seq + 1 if restored else FIRST_SEQ


class ChunkAccumulator:
    """Accumulates streamed DevOps_Agent response chunks in arrival order.

    The tool router appends every chunk of the streamed
    ``aidevops:SendMessage`` response and returns :meth:`result` as the
    single complete tool-result text (Req 3.3). The accumulated text is
    exactly the concatenation of all chunks in arrival order — including
    empty and unicode chunks, which are preserved verbatim (design
    Property 4).
    """

    def __init__(self) -> None:
        """Initialize an empty accumulator whose result is the empty string."""
        self._chunks: list[str] = []

    def append(self, chunk: str) -> None:
        """Record one streamed response chunk.

        Args:
            chunk: The next chunk of the streamed response, appended
                verbatim; may be empty and may contain any unicode text.
        """
        self._chunks.append(chunk)

    def result(self) -> str:
        """Return the complete accumulated response text.

        Returns:
            The exact concatenation of all appended chunks in arrival
            order (design Property 4); the empty string when no chunk has
            been appended.
        """
        return "".join(self._chunks)
