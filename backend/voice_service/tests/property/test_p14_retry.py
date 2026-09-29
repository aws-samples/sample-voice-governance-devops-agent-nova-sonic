# Feature: nova-sonic-support-portal, Property 14: Bounded retry contract
"""Property test: the retry helper honors the bounded retry contract.

**Validates: Requirements 2.7, 5.9, 5.10, 6.8, 10.7**

For any operation failure pattern and retry limit of 3: if the operation
succeeds on attempt k <= 4, the retry helper has made exactly k attempts
and reported success by returning the operation's result; if all 4 attempts
fail (initial + 3 retries), it has made exactly 4 attempts, reported
failure by re-raising the last exception unchanged, and logged retry
exhaustion with the specific exception class (Property 14). The suite also
pins the surrounding guarantees the design states for the helper: one
warning record tagged :data:`shared.retry.RETRY_FAILURE_EVENT` per failed
attempt, exactly one distinct error record tagged
:data:`shared.retry.RETRY_EXHAUSTION_EVENT` on exhaustion, one backoff
sleep between consecutive attempts with every jittered delay inside plus
or minus 50 percent of the 0.2 s / 0.8 s / 2.0 s base schedule, and
immediate propagation of non-retryable exceptions after exactly one
attempt with no retry logging.

Failure patterns run with the retryable exception classes named by the
validated requirements: ``SessionStoreError`` (transcript persistence,
Req 2.7), ``NotificationPublishError`` (AppSync Events publishing, Req 5.9
and 5.10), ``PushDeliveryError`` (Web Push delivery, Req 6.8), and
``TaskProtectionError`` (ECS task protection, Req 10.7).

Determinism comes from the helper's injection points: a recording ``sleep``
fake observes requested delays without real waiting, a per-example seeded
``random.Random`` fixes the jitter, and a private capturing logger collects
raw records. ``hypothesis.given`` cannot drive ``async def`` tests under
pytest-asyncio, so each example runs its coroutine to completion with
``asyncio.run`` from a synchronous test body — the simplest deterministic
pattern, giving every example a fresh event loop.
"""

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shared.retry import (
    DEFAULT_BACKOFF_SCHEDULE,
    RETRY_EXHAUSTION_EVENT,
    RETRY_FAILURE_EVENT,
    retry_async,
)

from app.exceptions import (
    GuardrailUnavailableError,
    NotificationPublishError,
    PortalError,
    PushDeliveryError,
    SessionStoreError,
    StreamOpenError,
    TaskProtectionError,
)

_RETRIES: Final[int] = 3
"""Retry limit fixed by the property statement (3 retries, 4 attempts)."""

_MAX_ATTEMPTS: Final[int] = _RETRIES + 1
"""Total attempts when every attempt fails: the initial one plus 3 retries."""

_JITTER_BOUNDS: Final[tuple[float, float]] = (0.5, 1.5)
"""Multiplicative jitter bounds around each base backoff delay."""

_RETRYABLE: Final[tuple[type[PortalError], ...]] = (
    SessionStoreError,
    NotificationPublishError,
    PushDeliveryError,
    TaskProtectionError,
)
"""Retryable classes passed to the helper, one per validated requirement."""

_FAILURE_FACTORIES: Final[tuple[Callable[[], PortalError], ...]] = (
    lambda: SessionStoreError("put transcript", session_id="vs-p14"),
    lambda: NotificationPublishError(503),
    lambda: PushDeliveryError(500, subscription_id="sub-p14"),
    lambda: TaskProtectionError("acquire"),
)
"""Builders of fresh retryable exception instances, one per failing attempt."""

_NON_RETRYABLE_FACTORIES: Final[tuple[Callable[[], PortalError], ...]] = (
    lambda: StreamOpenError("connection reset"),
    lambda: GuardrailUnavailableError("timeout"),
)
"""Builders of ``PortalError`` instances outside the retryable tuple."""


class _RecordingHandler(logging.Handler):
    """Collects raw log records instead of writing them anywhere."""

    def __init__(self) -> None:
        """Initialize the handler with an empty record collection."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record for later assertions.

        Args:
            record: The log record passing through the handler.
        """
        self.records.append(record)


def _make_logger() -> tuple[logging.Logger, _RecordingHandler]:
    """Build a fresh, isolated logger that captures raw records.

    The logger is a dedicated named logger reset on every call — handlers
    replaced, propagation disabled — so each hypothesis example observes
    only its own records and the root logger configuration is never
    touched.

    Returns:
        The logger and its recording handler.
    """
    handler = _RecordingHandler()
    logger = logging.getLogger("tests.property.p14")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


def _events(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    """Filter captured records down to those tagged with ``event``.

    Args:
        records: All records captured by the recording handler, in order.
        event: Expected value of the record's ``event`` extra field.

    Returns:
        The records whose ``event`` extra equals ``event``, in order.
    """
    return [record for record in records if getattr(record, "event", None) == event]


async def _check_contract(
    succeed_on: int | None,
    failure_factory: Callable[[], PortalError],
    seed: int,
) -> None:
    """Drive one failure pattern through the helper and assert the contract.

    Args:
        succeed_on: One-based attempt number on which the operation
            succeeds, or ``None`` when every attempt fails.
        failure_factory: Builds a fresh retryable exception instance for
            each failing attempt.
        seed: Seed for the injected jitter random source.

    Raises:
        AssertionError: If any facet of the bounded retry contract is
            violated for this failure pattern.
    """
    logger, handler = _make_logger()
    sleeps: list[float] = []
    raised: list[PortalError] = []
    attempts = 0
    success_marker = object()

    async def record_sleep(delay: float) -> None:
        """Record the requested backoff delay instead of waiting.

        Args:
            delay: Delay in seconds the retry helper asked to wait.
        """
        sleeps.append(delay)

    async def operation() -> object:
        """Fail until the success attempt is reached, then succeed.

        Returns:
            The success marker on attempt ``succeed_on``.

        Raises:
            PortalError: A fresh instance from ``failure_factory`` on every
                attempt before ``succeed_on``, or on all attempts when the
                pattern never succeeds.
        """
        nonlocal attempts
        attempts += 1
        if succeed_on is None or attempts < succeed_on:
            failure = failure_factory()
            raised.append(failure)
            raise failure
        return success_marker

    if succeed_on is None:
        with pytest.raises(PortalError) as excinfo:
            await retry_async(
                operation,
                retryable=_RETRYABLE,
                retries=_RETRIES,
                sleep=record_sleep,
                rng=random.Random(seed),
                logger=logger,
            )
        assert excinfo.value is raised[-1]
        expected_attempts = _MAX_ATTEMPTS
        expected_failures = _MAX_ATTEMPTS
    else:
        result = await retry_async(
            operation,
            retryable=_RETRYABLE,
            retries=_RETRIES,
            sleep=record_sleep,
            rng=random.Random(seed),
            logger=logger,
        )
        assert result is success_marker
        expected_attempts = succeed_on
        expected_failures = succeed_on - 1

    assert attempts == expected_attempts

    failures = _events(handler.records, RETRY_FAILURE_EVENT)
    exhaustions = _events(handler.records, RETRY_EXHAUSTION_EVENT)
    assert len(handler.records) == len(failures) + len(exhaustions)
    assert len(failures) == expected_failures
    assert all(record.levelno == logging.WARNING for record in failures)
    expected_attempt_numbers = list(range(1, expected_failures + 1))
    assert [
        getattr(record, "attempt", None) for record in failures
    ] == expected_attempt_numbers

    last_class_name = type(raised[-1]).__name__ if raised else None
    for record in failures:
        assert getattr(record, "exception_class", None) == last_class_name

    if succeed_on is None:
        assert len(exhaustions) == 1
        exhaustion = exhaustions[0]
        assert exhaustion.levelno == logging.ERROR
        assert getattr(exhaustion, "exception_class", None) == last_class_name
        assert getattr(exhaustion, "attempts", None) == _MAX_ATTEMPTS
    else:
        assert exhaustions == []

    assert len(sleeps) == expected_attempts - 1
    low, high = _JITTER_BOUNDS
    for index, delay in enumerate(sleeps):
        base = DEFAULT_BACKOFF_SCHEDULE[index]
        assert low * base <= delay <= high * base


async def _check_non_retryable(
    non_retryable_factory: Callable[[], PortalError],
    seed: int,
) -> None:
    """Assert a non-retryable exception propagates after a single attempt.

    Args:
        non_retryable_factory: Builds the non-retryable exception raised by
            the operation.
        seed: Seed for the injected jitter random source.

    Raises:
        AssertionError: If the helper retried, slept, or logged for the
            non-retryable exception.
    """
    logger, handler = _make_logger()
    sleeps: list[float] = []
    attempts = 0
    failure = non_retryable_factory()

    async def record_sleep(delay: float) -> None:
        """Record the requested backoff delay instead of waiting.

        Args:
            delay: Delay in seconds the retry helper asked to wait.
        """
        sleeps.append(delay)

    async def operation() -> object:
        """Raise the pre-built non-retryable exception on every invocation.

        Raises:
            PortalError: The pre-built non-retryable exception instance.
        """
        nonlocal attempts
        attempts += 1
        raise failure

    with pytest.raises(PortalError) as excinfo:
        await retry_async(
            operation,
            retryable=_RETRYABLE,
            retries=_RETRIES,
            sleep=record_sleep,
            rng=random.Random(seed),
            logger=logger,
        )

    assert excinfo.value is failure
    assert attempts == 1
    assert handler.records == []
    assert sleeps == []


@given(
    succeed_on=st.integers(min_value=1, max_value=_MAX_ATTEMPTS) | st.none(),
    failure_factory=st.sampled_from(_FAILURE_FACTORIES),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@settings(max_examples=100, deadline=None)
def test_bounded_retry_contract(
    succeed_on: int | None,
    failure_factory: Callable[[], PortalError],
    seed: int,
) -> None:
    """Success at attempt k makes exactly k attempts; exhaustion makes 4.

    For every failure pattern: success on attempt ``k <= 4`` returns the
    operation's result after exactly ``k`` attempts, with one warning
    record per failed attempt and no exhaustion record; a pattern that
    never succeeds re-raises the last exception unchanged after exactly 4
    attempts and 4 failure records, plus exactly one distinct exhaustion
    record naming the specific exception class. Exactly one jittered sleep
    separates consecutive attempts, each within plus or minus 50 percent
    of the 0.2 s / 0.8 s / 2.0 s base schedule.

    Args:
        succeed_on: One-based attempt on which the operation succeeds, or
            ``None`` for the all-fail pattern.
        failure_factory: Retryable exception factory drawn from the
            classes the validated requirements name.
        seed: Seed making the injected jitter random source deterministic.
    """
    asyncio.run(_check_contract(succeed_on, failure_factory, seed))


@given(
    non_retryable_factory=st.sampled_from(_NON_RETRYABLE_FACTORIES),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@settings(max_examples=100, deadline=None)
def test_non_retryable_exception_propagates_immediately(
    non_retryable_factory: Callable[[], PortalError],
    seed: int,
) -> None:
    """A non-retryable exception propagates after exactly one attempt.

    Exceptions outside the retryable classes are never retried: the helper
    makes exactly one attempt, performs no backoff sleep, emits no retry
    log records, and propagates the original exception instance unchanged.

    Args:
        non_retryable_factory: Factory for an exception class outside the
            retryable tuple.
        seed: Seed making the injected jitter random source deterministic.
    """
    asyncio.run(_check_non_retryable(non_retryable_factory, seed))
