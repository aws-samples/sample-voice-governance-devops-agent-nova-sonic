# Feature: nova-sonic-support-portal, Property 4: Streamed chunk accumulation equals concatenation
"""Property test: streamed chunk accumulation equals concatenation.

**Validates: Requirements 3.3**

For any sequence of streamed DevOps_Agent response chunks (including
empty and unicode chunks), the accumulated tool result text equals the
concatenation of all chunks in arrival order (Property 4). The suite
also pins the observation behavior the tool router relies on: a fresh
accumulator's result is the empty string, ``result`` is a pure
observation (two consecutive calls return equal text), and appending
after an intermediate ``result`` call continues the accumulation so the
final text reflects every append.

A secondary test drives the accumulator through the async shape it is
used in: the DevOps_Agent adapter exposes the streamed
``aidevops:SendMessage`` response as an async iterator that the tool
router consumes in a loop of appends. ``hypothesis.given`` cannot drive
``async def`` tests under pytest-asyncio, so each example runs its
coroutine to completion with ``asyncio.run`` from a synchronous test
body — the simplest deterministic pattern, giving every example a fresh
event loop.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.transcript import ChunkAccumulator

_CHUNKS: Final[st.SearchStrategy[list[str]]] = st.lists(st.text(), max_size=50)
"""Chunk sequences; ``st.text()`` includes empty and arbitrary-unicode chunks."""


@given(chunks=_CHUNKS)
@settings(max_examples=100, deadline=None)
def test_result_equals_concatenation_in_arrival_order(chunks: list[str]) -> None:
    """The accumulated result is exactly the in-order chunk concatenation.

    A fresh accumulator yields the empty string; after appending every
    chunk in arrival order the result equals ``"".join(chunks)`` — empty
    and unicode chunks preserved verbatim, never reordered; and
    ``result`` is a pure observation, so a second call returns equal
    text.

    Args:
        chunks: Arbitrary streamed response chunks, including empty and
            unicode chunks.
    """
    accumulator = ChunkAccumulator()
    assert accumulator.result() == ""

    for chunk in chunks:
        accumulator.append(chunk)

    first = accumulator.result()
    assert first == "".join(chunks)
    assert accumulator.result() == first


@given(prefix=_CHUNKS, suffix=_CHUNKS)
@settings(max_examples=100, deadline=None)
def test_appending_after_result_continues_the_accumulation(
    prefix: list[str], suffix: list[str]
) -> None:
    """Observing an intermediate result never disturbs later appends.

    After appending the prefix chunks, ``result`` returns the prefix
    concatenation; appending the suffix chunks afterwards yields the
    concatenation of both parts in arrival order, so an intermediate
    observation leaves the accumulation unaffected and the final result
    reflects every append.

    Args:
        prefix: Chunks appended before the intermediate observation.
        suffix: Chunks appended after the intermediate observation.
    """
    accumulator = ChunkAccumulator()
    for chunk in prefix:
        accumulator.append(chunk)
    assert accumulator.result() == "".join(prefix)

    for chunk in suffix:
        accumulator.append(chunk)
    assert accumulator.result() == "".join(prefix) + "".join(suffix)


async def _accumulate_streamed(chunks: list[str]) -> str:
    """Consume an async chunk stream the way the tool router does.

    Builds an async generator yielding the example's chunks in arrival
    order — the shape of the DevOps_Agent adapter's streamed-response
    iterator — and appends each consumed chunk to a fresh accumulator.

    Args:
        chunks: The chunks the fake stream yields, in arrival order.

    Returns:
        The accumulated result text after the stream is exhausted.
    """

    async def stream() -> AsyncIterator[str]:
        """Yield the example's chunks in arrival order.

        Yields:
            Each chunk in order, mimicking the adapter's streamed
            ``aidevops:SendMessage`` response iterator.
        """
        for chunk in chunks:
            yield chunk

    accumulator = ChunkAccumulator()
    async for chunk in stream():
        accumulator.append(chunk)
    return accumulator.result()


@given(chunks=_CHUNKS)
@settings(max_examples=100, deadline=None)
def test_async_stream_consumption_equals_concatenation(chunks: list[str]) -> None:
    """Consuming an async chunk stream preserves the concatenation equality.

    Driving the accumulator through the async path it serves in the tool
    router — an async iterator of streamed chunks consumed in a loop of
    appends — produces exactly ``"".join(chunks)``, the same text as the
    synchronous accumulation.

    Args:
        chunks: Arbitrary streamed response chunks, including empty and
            unicode chunks.
    """
    assert asyncio.run(_accumulate_streamed(chunks)) == "".join(chunks)
