"""Bounded FIFO buffer for engineer audio during session segmentation (Req 2.4).

While Session_Segmentation is in progress, the Voice_Service buffers up to
30 seconds of incoming engineer audio and delivers it to the new
Bedrock_Stream in the order it was received once that stream is ready. At
16 kHz, 16-bit (2-byte) mono LPCM, 30 seconds is exactly
``MAX_BUFFER_BYTES`` (960,000 bytes), derived below from named constants so
the bound is self-documenting.

This is a pure domain module: no I/O, no asyncio, no SDK imports.

Segmentation invariants (design Property 3):

- Flushing delivers exactly the buffered frames, byte-identical and in
  arrival order (FIFO).
- The buffer never holds more than ``max_bytes`` bytes of audio.
- On overflow the newest incoming frames are dropped and counted;
  already-buffered frames are never evicted, truncated, or reordered.

Partial-frame handling: a frame whose bytes would push the buffer past the
cap is dropped whole rather than truncated, so frame boundaries are always
preserved and every flushed frame is byte-identical to a frame that
arrived. Each incoming frame is judged against the remaining capacity at
its arrival instant. Callers observe drops through ``append`` returning
``False`` and through the cumulative ``dropped_frames`` /
``dropped_bytes`` counters, which the session manager reports in logs and
via the "please hold" status frame per the design.
"""

from typing import Final

SAMPLE_RATE_HZ: Final = 16_000
"""Engineer audio sample rate in hertz (16 kHz mono LPCM, Req 1.1)."""

BYTES_PER_SAMPLE: Final = 2
"""Bytes per mono audio sample (16-bit signed little-endian PCM)."""

MAX_BUFFER_SECONDS: Final = 30
"""Maximum seconds of audio buffered during segmentation (Req 2.4)."""

MAX_BUFFER_BYTES: Final = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * MAX_BUFFER_SECONDS
"""Default byte bound of the buffer: 960,000 bytes = 30 s at 16 kHz mono."""


class BoundedAudioBuffer:
    """Bounded FIFO of audio frames received while segmentation is in progress.

    Accepted frames are held in arrival order and returned byte-identical
    by :meth:`flush`. The buffered total never exceeds the ``max_bytes``
    capacity: a frame that would exceed it is dropped whole (drop-newest
    policy) and counted, and buffered frames are never reordered.
    """

    def __init__(self, max_bytes: int = MAX_BUFFER_BYTES) -> None:
        """Initialize an empty buffer with the given byte capacity.

        Args:
            max_bytes: Maximum total bytes the buffer may hold at once.
                Defaults to ``MAX_BUFFER_BYTES`` (30 seconds of 16 kHz,
                16-bit mono audio).
        """
        self._max_bytes = max_bytes
        self._frames: list[bytes] = []
        self._buffered_bytes = 0
        self._dropped_frames = 0
        self._dropped_bytes = 0

    def append(self, frame: bytes) -> bool:
        """Buffer a frame, or drop it whole if it would exceed the capacity.

        A frame that would push the buffered total past ``max_bytes`` is
        dropped in its entirety (never truncated) and counted in
        :attr:`dropped_frames` and :attr:`dropped_bytes`; frames already
        buffered are always preserved, so overflow drops the newest audio.

        Args:
            frame: Raw audio bytes (16 kHz, 16-bit mono LPCM) received
                while segmentation is in progress.

        Returns:
            ``True`` when the frame was buffered, ``False`` when it was
            dropped because it would exceed the remaining capacity.
        """
        if self._buffered_bytes + len(frame) > self._max_bytes:
            self._dropped_frames += 1
            self._dropped_bytes += len(frame)
            return False
        self._frames.append(frame)
        self._buffered_bytes += len(frame)
        return True

    def flush(self) -> list[bytes]:
        """Return all buffered frames in arrival order and empty the buffer.

        The returned frames are byte-identical to the frames accepted by
        :meth:`append`, in the exact order they arrived. After flushing,
        the buffer is empty and :attr:`buffered_bytes` is zero, so a
        second flush returns ``[]``. The drop counters are cumulative over
        the buffer's lifetime and are not reset by flushing.

        Returns:
            The buffered frames in arrival order; an empty list when
            nothing is buffered.
        """
        frames = self._frames
        self._frames = []
        self._buffered_bytes = 0
        return frames

    @property
    def buffered_bytes(self) -> int:
        """Total bytes currently buffered; never exceeds ``max_bytes``."""
        return self._buffered_bytes

    @property
    def dropped_frames(self) -> int:
        """Cumulative count of frames dropped whole on overflow."""
        return self._dropped_frames

    @property
    def dropped_bytes(self) -> int:
        """Cumulative total bytes of frames dropped on overflow."""
        return self._dropped_bytes
