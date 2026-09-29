"""Bounded async retry with exponential backoff, jitter, and exhaustion logging.

Implements the portal-wide retry policy from the design's error-handling
section: a retryable operation is attempted at most ``retries + 1`` times
(default 3 retries, so 4 attempts) with exponentially growing delays drawn
from the base schedule of 0.2 s / 0.8 s / 2.0 s between attempts. Only
exceptions matching the caller-supplied retryable classes are retried; any
other exception propagates immediately without a further attempt.

Jitter choice: each delay is the base schedule value multiplied by a factor
drawn uniformly from [0.5, 1.5] (plus/minus 50 percent), so the expected
delay equals the documented schedule value while concurrent callers spread
out instead of retrying in lockstep.

Every failed attempt produces one ``WARNING`` entry tagged with the
``event`` field :data:`RETRY_FAILURE_EVENT`, and exhausting all attempts
produces one distinct ``ERROR`` entry tagged :data:`RETRY_EXHAUSTION_EVENT`
that names the specific exception class, after which the last exception is
re-raised unchanged. Entries are emitted through the logger supplied by the
caller — for example the session-scoped logger from the Voice_Service, so
transcript-persistence failures carry the Voice_Session identifier
(Req 2.7) — and this module never configures or reconfigures logging;
``shared.logging`` owns the output format.

The helper backs every bounded-retry requirement in the design: transcript
persistence to the Session_Store (Req 2.7), AppSync Events publishing with
per-failure and exhaustion logging (Req 5.9, 5.10), Web Push delivery
(Req 6.8), and ECS scale-in task protection (Req 10.7). The ``sleep``,
``rng``, and ``logger`` injection points make the bounded retry contract
(design Property 14) deterministically testable without real waiting.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

__all__ = [
    "DEFAULT_BACKOFF_SCHEDULE",
    "DEFAULT_RETRIES",
    "RETRY_EXHAUSTION_EVENT",
    "RETRY_FAILURE_EVENT",
    "retry_async",
]

DEFAULT_RETRIES: Final[int] = 3
"""Default retry count after the first failed attempt (4 attempts total)."""

DEFAULT_BACKOFF_SCHEDULE: Final[tuple[float, ...]] = (0.2, 0.8, 2.0)
"""Base backoff delays in seconds before the first, second, and third retry."""

RETRY_FAILURE_EVENT: Final[str] = "retry.attempt_failed"
"""Value of the ``event`` log field on each per-failure log entry."""

RETRY_EXHAUSTION_EVENT: Final[str] = "retry.exhausted"
"""Value of the ``event`` log field on the distinct exhaustion log entry."""

_JITTER_FACTOR_RANGE: Final[tuple[float, float]] = (0.5, 1.5)
_MODULE_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)
_MODULE_RNG: Final[random.Random] = random.Random()


def _describe(operation: Callable[[], Awaitable[object]]) -> str:
    """Build a log-friendly name for a retried operation.

    Args:
        operation: The zero-argument async callable being retried.

    Returns:
        The callable's ``__qualname__`` when it has one, otherwise its
        ``repr``.
    """
    qualname = getattr(operation, "__qualname__", None)
    return qualname if isinstance(qualname, str) else repr(operation)


def _jittered_delay(
    backoff: Sequence[float],
    failure_count: int,
    rng: random.Random,
) -> float:
    """Compute the jittered backoff delay after a failed attempt.

    The base value is ``backoff[failure_count - 1]``; failure counts beyond
    the schedule reuse its final (largest) value, and an empty schedule
    yields no delay. Jitter multiplies the base by a factor drawn uniformly
    from [0.5, 1.5], keeping the expected delay equal to the base value.

    Args:
        backoff: Base delay schedule in seconds.
        failure_count: Number of failed attempts so far (1-based).
        rng: Random source supplying the jitter factor.

    Returns:
        The delay in seconds to wait before the next attempt.
    """
    if not backoff:
        return 0.0
    base = backoff[min(failure_count, len(backoff)) - 1]
    low, high = _JITTER_FACTOR_RANGE
    return base * rng.uniform(low, high)


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    retryable: type[Exception] | tuple[type[Exception], ...],
    retries: int = DEFAULT_RETRIES,
    backoff: Sequence[float] = DEFAULT_BACKOFF_SCHEDULE,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    logger: logging.Logger | None = None,
    name: str | None = None,
) -> T:
    """Run ``operation``, retrying retryable failures a bounded number of times.

    Implements the bounded retry contract (design Property 14): if the
    operation succeeds on attempt ``k`` with ``k <= retries + 1``, exactly
    ``k`` attempts have been made and the result is returned; if every
    attempt fails, exactly ``retries + 1`` attempts have been made, each
    failure has produced one warning entry, one distinct exhaustion record
    naming the specific exception class has been emitted, and the last
    exception is re-raised unchanged. Exceptions that do not match
    ``retryable`` propagate immediately without further attempts or retry
    logging, and ``asyncio.CancelledError`` derives from ``BaseException``,
    so cancellation is never swallowed.

    Args:
        operation: Zero-argument async callable to run; wrap calls that
            need arguments in ``functools.partial`` or a closure.
        retryable: Exception class, or tuple of classes, that may be
            retried — for example ``SessionStoreError`` for transcript
            persistence (Req 2.7) or ``NotificationPublishError`` for
            AppSync Events publishing (Req 5.9).
        retries: Maximum number of additional attempts after the first
            failure. Defaults to :data:`DEFAULT_RETRIES` (3 retries, so 4
            attempts); values below zero are treated as zero (a single
            attempt).
        backoff: Base delay schedule in seconds applied between attempts,
            :data:`DEFAULT_BACKOFF_SCHEDULE` (0.2 / 0.8 / 2.0) by default;
            failures beyond the schedule length reuse its final value.
        sleep: Awaitable delay function, ``asyncio.sleep`` by default.
            Tests inject a recording fake to observe delays without real
            waiting.
        rng: Random source for the plus/minus 50 percent jitter factor; a
            module-level ``random.Random`` is used when omitted. Tests
            inject a seeded instance for determinism.
        logger: Logger receiving the per-failure and exhaustion entries;
            pass a session-scoped logger so entries carry the
            Voice_Session identifier (Req 2.7). Defaults to this module's
            logger.
        name: Human-readable operation name used in log entries; derived
            from the callable when omitted.

    Returns:
        The value returned by the first successful invocation of
        ``operation``.

    Raises:
        Exception: The last retryable exception, re-raised unchanged once
            all attempts are exhausted; non-retryable exceptions raised by
            ``operation`` propagate immediately.
    """
    attempts_allowed = max(0, retries) + 1
    retryable_classes: tuple[type[Exception], ...] = (
        retryable if isinstance(retryable, tuple) else (retryable,)
    )
    active_rng = rng if rng is not None else _MODULE_RNG
    log = logger if logger is not None else _MODULE_LOGGER
    operation_name = name if name is not None else _describe(operation)

    attempt = 0
    while True:
        attempt += 1
        try:
            result = await operation()
        except retryable_classes as exc:
            exception_class = type(exc).__name__
            log.warning(
                "Attempt %d of %d for %s failed with %s",
                attempt,
                attempts_allowed,
                operation_name,
                exception_class,
                extra={
                    "event": RETRY_FAILURE_EVENT,
                    "operation": operation_name,
                    "exception_class": exception_class,
                    "attempt": attempt,
                    "max_attempts": attempts_allowed,
                    "will_retry": attempt < attempts_allowed,
                },
            )
            if attempt >= attempts_allowed:
                log.exception(
                    "Retry exhausted for %s after %d attempts; last failure %s",
                    operation_name,
                    attempt,
                    exception_class,
                    extra={
                        "event": RETRY_EXHAUSTION_EVENT,
                        "operation": operation_name,
                        "exception_class": exception_class,
                        "attempts": attempt,
                    },
                )
                raise
            await sleep(_jittered_delay(backoff, attempt, active_rng))
        else:
            return result
