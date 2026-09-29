# Feature: nova-sonic-support-portal, Property 17: Task protection tracks live sessions
"""Property test: ECS task scale-in protection tracks live Voice_Sessions.

**Validates: Requirements 10.3, 10.4, 19.6, 19.7**

For any interleaving of Voice_Session starts and ends on a task, scale-in
protection is enabled whenever the live-session count is greater than
zero, and is released (within the 60-second bound, under a fake clock)
when the count returns to zero — so a task hosting live sessions is never
scale-in eligible (Req 10.3, 19.6) and an idle task always becomes
eligible (Req 10.4, 19.7; design Property 17).

The main property drives
:class:`app.orchestration.protection_manager.ProtectionManager` with
random start/end interleavings, advancing a :class:`tests.fakes.FakeClock`
between operations, and checks after every settled operation that:

- the manager's live-session count matches a reference count that models
  an end on an idle task as an ignored underflow;
- the manager's ``protected`` flag and the fake port's recorded state
  both equal ``count > 0`` (Req 10.3, 19.6, 19.7);
- the port call log matches the model exactly: one acquire per
  idle-to-busy (0 -> 1) transition carrying the configured expiry, one
  release per busy-to-idle (1 -> 0) transition, and nothing else;
- the release is recorded at the fake clock's current instant — the
  manager releases synchronously inside ``session_ended`` — which the
  test also checks formally against the 60-second bound (Req 10.4).

The companion failure property injects an acquire failure budget of 1-8
into :class:`tests.fakes.FakeTaskProtection` under the standard retry
policy (3 retries, 4 attempts per call) and models the exact outcome of
three consecutive starts: with budget ``b`` the first start succeeds iff
``b <= 3``; on exhaustion (``b >= 4``) readiness degrades while the count
stays positive — the invariant weakens to ``protected == (count > 0) or
not is_ready`` — and the next start retries the acquire with the
remaining budget, succeeding iff ``b <= 7``, and always by the third
start since ``b <= 8``, restoring readiness and protection. Cumulative
acquire attempts follow ``min(b + 1, 4k)`` after ``k`` starts, because
every acquire call makes up to 4 attempts and stops on the first success.

Determinism: the injected sleep returns instantly for retry backoff
delays (at most 3 seconds jittered) and parks forever — cancellably — on
the 300-second rolling-refresh interval, so the refresh loop never fires
during an example and every recorded port call belongs to a driven
transition; the clock only moves when the test advances it, so recorded
timestamps are exact. ``hypothesis.given`` cannot drive ``async def``
tests under pytest-asyncio, so each example runs its coroutine to
completion with ``asyncio.run`` from a synchronous test body — the same
pattern as the Property 7 and 14 suites, giving every example a fresh
event loop.
"""

import asyncio
import logging
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from app.orchestration.protection_manager import ProtectionManager
from tests.fakes import FakeClock, FakeTaskProtection

_EXPIRY_MINUTES: Final[int] = 15
"""Protection expiry passed to the manager and expected on acquire records."""

_REFRESH_INTERVAL_SECONDS: Final[float] = 300.0
"""Rolling-refresh interval; parked forever by the selective sleep fake."""

_RETRIES: Final[int] = 3
"""Retry budget per protection call (3 retries, 4 attempts), per Req 10.7."""

_ATTEMPTS_PER_CALL: Final[int] = _RETRIES + 1
"""Maximum acquire attempts a single protection call makes."""

_RELEASE_BOUND_SECONDS: Final[float] = 60.0
"""Release deadline after the last session ends (Req 10.4)."""

_BLOCK_THRESHOLD_SECONDS: Final[float] = 10.0
"""Sleep-delay threshold separating retry backoff (below: instant) from the
refresh interval (at or above: parked forever); jittered backoff tops out at
3 seconds, the refresh interval is 300 seconds."""

_MAX_OPS: Final[int] = 30
"""Maximum number of interleaved start/end operations per example."""

_MAX_ADVANCE_SECONDS: Final[float] = 30.0
"""Maximum fake-clock advance drawn between consecutive operations."""

_RECOVERY_STARTS: Final[int] = 3
"""Session starts driven in the failure property; enough to exhaust the
largest failure budget (8) and always reach a confirmed acquire."""

_MAX_FAILURE_BUDGET: Final[int] = 8
"""Largest injected acquire failure budget: two full exhaustions (4 + 4),
recovered by the third start's first attempt."""

_OPS_STRATEGY: Final = st.lists(
    st.tuples(
        st.sampled_from(("start", "end")),
        st.floats(min_value=0.0, max_value=_MAX_ADVANCE_SECONDS),
    ),
    max_size=_MAX_OPS,
)
"""Interleavings of (operation, clock-advance-seconds) pairs."""

_RECOVERY_ADVANCES_STRATEGY: Final = st.lists(
    st.floats(min_value=0.0, max_value=_MAX_ADVANCE_SECONDS),
    min_size=_RECOVERY_STARTS * 2,
    max_size=_RECOVERY_STARTS * 2,
)
"""Clock advances before each of the failure scenario's 6 fixed steps."""


async def _selective_sleep(delay: float) -> None:
    """Return instantly for retry backoff; park forever on the refresh interval.

    The manager wires one sleep callable into both the shared retry helper
    (jittered backoff of at most 3 seconds) and the rolling-refresh loop
    (300 seconds), so the delay value discriminates the two: backoff
    returns immediately and the refresh interval waits on an event that is
    never set. The park is cancellable, so ``session_ended`` cancelling
    the refresh task — and ``asyncio.run`` tearing the loop down — always
    proceeds cleanly.

    Args:
        delay: Requested delay in seconds; compared against
            :data:`_BLOCK_THRESHOLD_SECONDS`.
    """
    if delay >= _BLOCK_THRESHOLD_SECONDS:
        await asyncio.Event().wait()


def _quiet_logger() -> logging.Logger:
    """Build a logger that swallows the manager's lifecycle entries.

    The suite asserts on manager state and recorded port calls, not on
    logs, so a dedicated non-propagating logger with a ``NullHandler``
    keeps expected retry warnings and exhaustion records out of the test
    output without touching global logging configuration.

    Returns:
        A reusable, isolated, silent logger.
    """
    logger = logging.getLogger("tests.property.p17")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def _make_manager(fake: FakeTaskProtection) -> ProtectionManager:
    """Build a manager wired to the fake port with deterministic timing.

    Args:
        fake: Fake task-protection port recording timestamped attempts.

    Returns:
        A :class:`ProtectionManager` using the test expiry, the standard
        retry budget, a silent logger, and the selective sleep that makes
        retries instant and parks the refresh loop.
    """
    return ProtectionManager(
        fake,
        expiry_minutes=_EXPIRY_MINUTES,
        refresh_interval_seconds=_REFRESH_INTERVAL_SECONDS,
        retries=_RETRIES,
        logger=_quiet_logger(),
        sleep=_selective_sleep,
    )


async def _check_interleaving(ops: list[tuple[str, float]]) -> None:
    """Drive one start/end interleaving and assert Property 17 throughout.

    Replays the drawn operations against a fresh manager, fake port, and
    fake clock, maintaining a reference model (session count and expected
    port call log) alongside, then drains any sessions left live so every
    example also exercises the final release. After each settled operation
    the manager, the fake port, and the model must agree.

    Args:
        ops: Drawn interleaving as (operation, clock-advance-seconds)
            pairs; the clock advances by the drawn amount before each
            operation is applied.

    Raises:
        AssertionError: If any facet of Property 17 is violated after any
            settled operation.
    """
    clock = FakeClock()
    fake = FakeTaskProtection(clock)
    manager = _make_manager(fake)
    expected_count = 0
    expected_calls: list[tuple[str, int | None, float]] = []

    async def apply_op(op: str) -> None:
        """Apply one operation, update the model, and assert the invariant.

        Args:
            op: ``"start"`` for ``session_started``, ``"end"`` for
                ``session_ended``.
        """
        nonlocal expected_count
        now = clock.now()
        if op == "start":
            await manager.session_started()
            if expected_count == 0:
                expected_calls.append(("acquire", _EXPIRY_MINUTES, now))
            expected_count += 1
        else:
            await manager.session_ended()
            if expected_count > 0:
                expected_count -= 1
                if expected_count == 0:
                    expected_calls.append(("release", None, now))
                    released = fake.calls[-1]
                    assert released == ("release", None, now)
                    assert released[2] - now <= _RELEASE_BOUND_SECONDS
        assert manager.live_sessions == expected_count
        assert manager.protected == (expected_count > 0)
        assert fake.protected == manager.protected
        assert manager.is_ready is True
        assert fake.calls == expected_calls

    for op, advance_seconds in ops:
        clock.advance(advance_seconds)
        await apply_op(op)
    while expected_count > 0:
        await apply_op("end")


async def _check_acquire_failure_recovery(
    failure_budget: int, advances: list[float]
) -> None:
    """Drive acquire failures through starts and assert readiness tracking.

    With ``failure_budget`` injected acquire failures and 4 attempts per
    protection call, start ``k`` leaves the manager confirmed iff
    ``failure_budget + 1 <= 4k``: exhaustion degrades readiness while the
    session count stays positive (the Property 17 invariant weakens to
    ``protected == (count > 0) or not is_ready``), and the first
    successful re-acquire on a later start restores readiness and
    protection. Draining all sessions afterwards releases protection
    exactly once, leaving the recovered task scale-in eligible.

    Args:
        failure_budget: Number of initial acquire attempts the fake port
            fails before succeeding again (1 to 8).
        advances: Clock advance in seconds applied before each of the
            three starts and three ends, so recorded timestamps vary
            across examples.

    Raises:
        AssertionError: If readiness, protection, or the recorded acquire
            attempts diverge from the model at any step.
    """
    clock = FakeClock()
    fake = FakeTaskProtection(clock, fail_acquire=failure_budget)
    manager = _make_manager(fake)

    for start_number in range(1, _RECOVERY_STARTS + 1):
        clock.advance(advances[start_number - 1])
        await manager.session_started()
        acquire_attempts = [call for call in fake.calls if call[0] == "acquire"]
        assert len(acquire_attempts) == min(
            failure_budget + 1, start_number * _ATTEMPTS_PER_CALL
        )
        assert all(call[1] == _EXPIRY_MINUTES for call in acquire_attempts)
        confirmed = failure_budget + 1 <= start_number * _ATTEMPTS_PER_CALL
        assert manager.live_sessions == start_number
        assert manager.is_ready is confirmed
        assert manager.protected is confirmed
        assert fake.protected is confirmed
        assert manager.protected == (manager.live_sessions > 0) or (
            not manager.is_ready
        )
        assert not any(call[0] == "release" for call in fake.calls)

    assert manager.is_ready is True
    assert manager.protected is True
    assert fake.protected is True

    for sessions_left in range(_RECOVERY_STARTS - 1, -1, -1):
        clock.advance(advances[2 * _RECOVERY_STARTS - 1 - sessions_left])
        await manager.session_ended()
        assert manager.live_sessions == sessions_left
        assert manager.protected is (sessions_left > 0)
        assert fake.protected is (sessions_left > 0)
        assert manager.is_ready is True

    acquire_attempts = [call for call in fake.calls if call[0] == "acquire"]
    assert len(acquire_attempts) == failure_budget + 1
    releases = [call for call in fake.calls if call[0] == "release"]
    assert releases == [("release", None, clock.now())]
    assert fake.calls[-1] == ("release", None, clock.now())


@given(ops=_OPS_STRATEGY)
@settings(max_examples=100, deadline=None)
def test_protection_tracks_live_sessions(ops: list[tuple[str, float]]) -> None:
    """Protection is held exactly while the live-session count is positive.

    For every interleaving of session starts and ends: the manager's count
    matches the underflow-ignoring reference count, protection (manager
    flag and fake port state alike) equals ``count > 0`` after every
    settled operation (Req 10.3, 19.6, 19.7), the port sees exactly one
    acquire per 0 -> 1 transition and one release per 1 -> 0 transition,
    and each release lands at the instant the count reaches zero — inside
    the 60-second bound (Req 10.4).

    Args:
        ops: Drawn interleaving as (operation, clock-advance-seconds)
            pairs.
    """
    asyncio.run(_check_interleaving(ops))


@given(
    failure_budget=st.integers(min_value=1, max_value=_MAX_FAILURE_BUDGET),
    advances=_RECOVERY_ADVANCES_STRATEGY,
)
@settings(max_examples=100, deadline=None)
def test_acquire_exhaustion_degrades_readiness_until_recovery(
    failure_budget: int,
    advances: list[float],
) -> None:
    """Acquire exhaustion degrades readiness; a later success restores it.

    For every failure budget of 1 to 8: each start's confirmation state
    follows the retry arithmetic (start ``k`` confirms iff
    ``budget + 1 <= 4k``), exhaustion leaves the task unprotected but
    not-ready while sessions are live (the weakened Property 17
    invariant), recovery restores readiness and protection with exactly
    ``budget + 1`` cumulative acquire attempts, and draining every session
    releases protection exactly once.

    Args:
        failure_budget: Drawn number of leading acquire attempts the fake
            port fails.
        advances: Drawn clock advances applied before each start and end,
            varying the recorded timestamps across examples.
    """
    asyncio.run(_check_acquire_failure_recovery(failure_budget, advances))
