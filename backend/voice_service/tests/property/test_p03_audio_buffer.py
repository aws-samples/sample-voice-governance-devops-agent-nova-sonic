# Feature: nova-sonic-support-portal, Property 3: Segmentation audio buffer is a bounded FIFO
"""Property test: segmentation audio buffer is a bounded FIFO.

**Validates: Requirements 2.4**

For any sequence of audio frames received while segmentation is in
progress, flushing the buffer delivers exactly the buffered frames,
byte-identical and in arrival order, and the buffer never holds more than
30 seconds of audio — excess frames are dropped newest-first and counted,
never reordered (Property 3).

Every example is checked against a greedy reference model: a frame is
accepted iff the buffered total plus the frame's length stays within the
capacity at its arrival instant. The bound is inclusive (a frame landing
exactly on the cap is accepted), zero-length frames always fit, an
oversized frame is dropped whole rather than truncated, and frames
arriving after a drop are still accepted when they fit. Most tests use
small custom capacities so overflow is exercised richly — the production
960,000-byte cap would rarely overflow under random data — while one test
drives the real ``MAX_BUFFER_BYTES`` bound with large synthesized frames.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.audio_buffer import (
    BYTES_PER_SAMPLE,
    MAX_BUFFER_BYTES,
    MAX_BUFFER_SECONDS,
    SAMPLE_RATE_HZ,
    BoundedAudioBuffer,
)


def _simulate(
    frames: list[bytes], max_bytes: int
) -> tuple[list[bytes], list[bool], int, int]:
    """Greedy reference model of the bounded FIFO buffer.

    Frames are accepted in arrival order while the running buffered total
    plus the incoming frame's length stays within ``max_bytes`` (inclusive
    bound); a frame that does not fit is dropped whole and counted, and
    later frames that fit are still accepted.

    Args:
        frames: The audio frames in arrival order.
        max_bytes: The buffer's byte capacity.

    Returns:
        A tuple of the expected accepted frames in arrival order, the
        expected per-frame ``append`` verdicts, the expected dropped-frame
        count, and the expected dropped-byte total.
    """
    accepted: list[bytes] = []
    verdicts: list[bool] = []
    buffered = 0
    dropped_frames = 0
    dropped_bytes = 0
    for frame in frames:
        if buffered + len(frame) <= max_bytes:
            accepted.append(frame)
            verdicts.append(True)
            buffered += len(frame)
        else:
            verdicts.append(False)
            dropped_frames += 1
            dropped_bytes += len(frame)
    return accepted, verdicts, dropped_frames, dropped_bytes


@given(
    frames=st.lists(st.binary(min_size=0, max_size=64), max_size=50),
    max_bytes=st.integers(0, 500),
)
@settings(max_examples=100, deadline=None)
def test_buffer_matches_greedy_fifo_model(frames: list[bytes], max_bytes: int) -> None:
    """Appends, drops, counters, and flush order match the reference model.

    Every ``append`` verdict equals the model's greedy fit decision, the
    buffered total never exceeds the capacity at any point, ``flush``
    returns exactly the accepted frames byte-identical and in arrival
    order (never reordered, never truncated), the drop counters equal the
    model's, and flushing empties the buffer (a second flush returns
    ``[]``) without resetting the cumulative drop counters.

    Args:
        frames: Arbitrary audio frames, including zero-length frames.
        max_bytes: Small byte capacity so overflow occurs richly.
    """
    (
        expected_accepted,
        expected_verdicts,
        expected_dropped_frames,
        expected_dropped_bytes,
    ) = _simulate(frames, max_bytes)

    buffer = BoundedAudioBuffer(max_bytes=max_bytes)
    for frame, expected_verdict in zip(frames, expected_verdicts, strict=True):
        assert buffer.append(frame) == expected_verdict
        assert buffer.buffered_bytes <= max_bytes

    assert buffer.buffered_bytes == sum(len(frame) for frame in expected_accepted)
    assert buffer.dropped_frames == expected_dropped_frames
    assert buffer.dropped_bytes == expected_dropped_bytes

    assert buffer.flush() == expected_accepted
    assert buffer.buffered_bytes == 0
    assert buffer.flush() == []
    assert buffer.dropped_frames == expected_dropped_frames
    assert buffer.dropped_bytes == expected_dropped_bytes


@given(sizes=st.lists(st.integers(0, 400_000), max_size=12))
@settings(max_examples=100, deadline=None)
def test_default_cap_bounds_buffer_to_thirty_seconds(sizes: list[int]) -> None:
    """The default capacity is 30 s of audio and is never exceeded.

    ``MAX_BUFFER_BYTES`` equals 30 seconds at 16 kHz, 16-bit mono
    (960,000 bytes); with frames large enough to overflow it, every
    ``append`` verdict and both drop counters match the reference model,
    the buffered total never exceeds the cap at any point, and flush
    preserves arrival order byte-identically.

    Args:
        sizes: Frame sizes up to 400,000 bytes so a handful of frames
            overflows the 960,000-byte default capacity.
    """
    assert MAX_BUFFER_BYTES == MAX_BUFFER_SECONDS * SAMPLE_RATE_HZ * BYTES_PER_SAMPLE

    frames = [bytes([index % 256]) * size for index, size in enumerate(sizes)]
    (
        expected_accepted,
        expected_verdicts,
        expected_dropped_frames,
        expected_dropped_bytes,
    ) = _simulate(frames, MAX_BUFFER_BYTES)

    buffer = BoundedAudioBuffer()
    for frame, expected_verdict in zip(frames, expected_verdicts, strict=True):
        assert buffer.append(frame) == expected_verdict
        assert buffer.buffered_bytes <= MAX_BUFFER_BYTES

    assert buffer.flush() == expected_accepted
    assert buffer.dropped_frames == expected_dropped_frames
    assert buffer.dropped_bytes == expected_dropped_bytes


@given(
    operations=st.lists(st.none() | st.binary(min_size=0, max_size=64), max_size=60),
    max_bytes=st.integers(0, 500),
)
@settings(max_examples=100, deadline=None)
def test_interleaved_append_flush_cycles_stay_fifo(
    operations: list[bytes | None], max_bytes: int
) -> None:
    """Interleaved append/flush cycles keep FIFO order per cycle.

    Across any interleaving of appends and flushes, each flush returns
    exactly the frames accepted since the previous flush, byte-identical
    and in arrival order; flushing frees the full capacity for later
    frames; the buffered total never exceeds the capacity; and the drop
    counters accumulate across flushes without ever resetting.

    Args:
        operations: The operation sequence — a ``bytes`` value appends
            that frame, ``None`` flushes the buffer.
        max_bytes: Small byte capacity so overflow occurs richly.
    """
    buffer = BoundedAudioBuffer(max_bytes=max_bytes)
    pending: list[bytes] = []
    pending_bytes = 0
    expected_dropped_frames = 0
    expected_dropped_bytes = 0

    for operation in operations:
        if operation is None:
            assert buffer.flush() == pending
            pending = []
            pending_bytes = 0
        else:
            fits = pending_bytes + len(operation) <= max_bytes
            assert buffer.append(operation) == fits
            if fits:
                pending.append(operation)
                pending_bytes += len(operation)
            else:
                expected_dropped_frames += 1
                expected_dropped_bytes += len(operation)
        assert buffer.buffered_bytes == pending_bytes
        assert buffer.buffered_bytes <= max_bytes
        assert buffer.dropped_frames == expected_dropped_frames
        assert buffer.dropped_bytes == expected_dropped_bytes

    assert buffer.flush() == pending


@given(
    frames=st.lists(st.binary(min_size=0, max_size=64), max_size=20),
    max_bytes=st.integers(0, 500),
)
@settings(max_examples=100, deadline=None)
def test_bound_is_inclusive_and_full_buffer_drops_only_nonempty(
    frames: list[bytes], max_bytes: int
) -> None:
    """A frame landing exactly on the cap fits; a full buffer drops audio.

    After any prefix of appends, a frame that exactly fills the remaining
    capacity is accepted (the bound is inclusive), any further non-empty
    frame is dropped whole and counted, and zero-length frames are always
    accepted — even at full capacity.

    Args:
        frames: Arbitrary prefix frames appended before the filler.
        max_bytes: Small byte capacity so the exact-fill filler is cheap
            to build.
    """
    buffer = BoundedAudioBuffer(max_bytes=max_bytes)
    for frame in frames:
        buffer.append(frame)

    filler = b"\xff" * (max_bytes - buffer.buffered_bytes)
    assert buffer.append(filler) is True
    assert buffer.buffered_bytes == max_bytes

    dropped_before = buffer.dropped_frames
    assert buffer.append(b"\x00") is False
    assert buffer.dropped_frames == dropped_before + 1
    assert buffer.append(b"") is True
    assert buffer.buffered_bytes == max_bytes

    assert buffer.flush()[-2:] == [filler, b""]
