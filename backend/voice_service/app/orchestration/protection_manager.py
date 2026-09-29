"""Live-session registry driving ECS task scale-in protection.

Implements the design's task protection manager: a per-task registry of
live Voice_Sessions that drives ``TaskProtectionPort`` so the hosting ECS
task is protected from scale-in exactly while it serves live sessions.

Behavior, by requirement:

- **Acquire on idle-to-busy**: when the live-session count goes 0 -> 1 the
  manager enables scale-in protection, so a task hosting at least one live
  Voice_Session keeps protection enabled (Req 10.3, 19.6).
- **Release on busy-to-idle**: when the count returns to 0 the manager
  releases protection synchronously inside
  :meth:`ProtectionManager.session_ended` — immediate in the common case.
  Worst case it waits for one in-flight refresh iteration ahead of it on
  the lock plus its own retry backoff (about 4.5 s each under the default
  jittered 0.2/0.8/2.0 s schedule), roughly 9 s — well inside the
  60-second bound (Req 10.4, 19.7).
- **Rolling refresh**: while protected with live sessions, a background
  task re-acquires protection every ``refresh_interval_seconds`` (default
  300 s, well inside the default 15-minute expiry), resetting the expiry
  so long sessions never outlive it (Req 10.3).
- **Bounded retries and readiness**: every port call runs under the shared
  bounded retry policy (``retries`` retries, default 3, so 4 attempts).
  When an acquire or refresh exhausts its retries,
  :attr:`ProtectionManager.is_ready` flips to ``False`` so ``/healthz``
  reports 503 and the ALB stops routing new sessions to this task, until a
  later successful acquire confirms protection and restores readiness
  (Req 10.7). Existing sessions continue undisturbed.

Concurrency: one ``asyncio.Lock`` serializes count transitions together
with their port calls, so after every settled successful transition the
invariant *protected == (live count > 0)* (design Property 17) holds and
interleaved transitions can never race it. Port calls are short and
bounded, so holding the lock across them is safe; the refresh loop's long
interval sleep happens outside the lock so transitions never wait on it.
Cancelling the refresh task suppresses ``CancelledError`` only at the
cancellation site — never inside the loop body — so external cancellation
is never swallowed.

Documented decisions:

- A failed **release** does *not* degrade readiness: Requirement 10.7's
  503 contract covers *enabling* protection only. The exhaustion is logged
  (:data:`RELEASE_FAILED_EVENT`) and protection self-heals when its expiry
  lapses, at which point the task becomes scale-in eligible again.
- After a **refresh** exhaustion the manager keeps ``protected`` set (the
  ECS-side protection remains enabled until its expiry) and keeps the
  refresh loop running, so the next interval re-attempt restores readiness
  without operator action. A first session starting on an *unprotected*
  task (an earlier initial acquire exhausted its retries) re-attempts the
  acquire, which is the recovery path that restores readiness there.

This module performs no I/O of its own and imports no SDK (Req 17.6); the
ECS agent call belongs to the ``adapters.ecs_task_protection`` adapter
behind the port.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Final

from shared.retry import retry_async

from app.exceptions import TaskProtectionError
from app.ports.task_protection import TaskProtectionPort

__all__ = [
    "ACQUIRED_EVENT",
    "DEFAULT_EXPIRY_MINUTES",
    "DEFAULT_PROTECTION_RETRIES",
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "READINESS_DEGRADED_EVENT",
    "READINESS_RESTORED_EVENT",
    "RELEASED_EVENT",
    "RELEASE_FAILED_EVENT",
    "SESSION_UNDERFLOW_EVENT",
    "ProtectionManager",
]

DEFAULT_EXPIRY_MINUTES: Final[int] = 15
"""Default protection expiry passed to acquire, bounding a missed release."""

DEFAULT_REFRESH_INTERVAL_SECONDS: Final[float] = 300.0
"""Default rolling-refresh cadence; 5 minutes, well inside the expiry."""

DEFAULT_PROTECTION_RETRIES: Final[int] = 3
"""Default retry budget per protection call (3 retries, 4 attempts)."""

ACQUIRED_EVENT: Final[str] = "task_protection.acquired"
"""Value of the ``event`` log field when protection is confirmed enabled."""

RELEASED_EVENT: Final[str] = "task_protection.released"
"""Value of the ``event`` log field when protection is confirmed released."""

READINESS_DEGRADED_EVENT: Final[str] = "task_protection.readiness_degraded"
"""Value of the ``event`` log field when acquire exhaustion degrades readiness."""

READINESS_RESTORED_EVENT: Final[str] = "task_protection.readiness_restored"
"""Value of the ``event`` log field when a confirmed acquire restores readiness."""

RELEASE_FAILED_EVENT: Final[str] = "task_protection.release_failed"
"""Value of the ``event`` log field when release exhaustion leaves self-heal."""

SESSION_UNDERFLOW_EVENT: Final[str] = "task_protection.session_underflow"
"""Value of the ``event`` log field when a session end has no matching start."""

_MODULE_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class ProtectionManager:
    """Counts live Voice_Sessions and drives task scale-in protection.

    The session manager reports each session start and end; this class
    owns the acquire/refresh/release policy over the injected
    ``TaskProtectionPort`` (Req 10.3, 10.4, 19.6, 19.7) and the readiness
    flag that the ``/healthz`` endpoint combines with drain state
    (Req 10.7). :meth:`session_started` and :meth:`session_ended` never
    raise ``TaskProtectionError``: failures are retried, logged, and
    reflected in :attr:`is_ready` so sessions themselves are never
    disrupted by protection trouble.
    """

    def __init__(
        self,
        protection: TaskProtectionPort,
        *,
        expiry_minutes: int = DEFAULT_EXPIRY_MINUTES,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        retries: int = DEFAULT_PROTECTION_RETRIES,
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Initialize an idle, ready manager with no live sessions.

        Args:
            protection: Port to the ECS task protection state; the real
                adapter in production, ``FakeTaskProtection`` in tests.
            expiry_minutes: Protection expiry passed to every acquire and
                refresh, bounding the impact of a missed release.
            refresh_interval_seconds: Delay between rolling expiry
                refreshes for long sessions; must be comfortably smaller
                than ``expiry_minutes`` so protection never lapses while
                sessions are live.
            retries: Retry budget per protection call under the shared
                bounded retry policy (Req 10.7).
            logger: Logger for protection lifecycle and failure entries;
                this module's logger when omitted.
            sleep: Awaitable delay used for both retry backoff and the
                refresh interval, ``asyncio.sleep`` by default. Tests
                inject an instant fake so nothing really waits.
        """
        self._protection = protection
        self._expiry_minutes = expiry_minutes
        self._refresh_interval_seconds = refresh_interval_seconds
        self._retries = retries
        self._logger = logger if logger is not None else _MODULE_LOGGER
        self._sleep = sleep
        self._live_sessions = 0
        self._protected = False
        self._ready = True
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def is_ready(self) -> bool:
        """Return the readiness flag consumed by the ``/healthz`` endpoint.

        Returns:
            ``False`` from the moment a protection acquire (initial,
            recovery, or rolling refresh) exhausts its retries until a
            later acquire confirms protection, directing ``/healthz`` to
            report 503 so no new sessions are routed here (Req 10.7);
            ``True`` otherwise, including at startup.
        """
        return self._ready

    @property
    def live_sessions(self) -> int:
        """Return the number of live Voice_Sessions on this task.

        Returns:
            The current registry count: starts reported minus ends
            reported, never negative.
        """
        return self._live_sessions

    @property
    def protected(self) -> bool:
        """Return the last confirmed scale-in protection state.

        Returns:
            ``True`` after a confirmed acquire and until a confirmed
            release. Stays ``True`` after a failed release (the ECS-side
            protection remains enabled until its expiry lapses) and after
            a failed refresh, both of which are unconfirmed transitions.
        """
        return self._protected

    async def session_started(self) -> None:
        """Register a session start, acquiring protection when needed.

        Increments the live-session count. When the count goes 0 -> 1 —
        or the task is unprotected because an earlier acquire exhausted
        its retries — protection is acquired under the bounded retry
        policy (Req 10.3, 19.6). Success confirms protection, restores
        readiness, and starts the rolling refresh loop; exhaustion
        degrades readiness so ``/healthz`` reports 503 until protection
        is confirmed (Req 10.7). The count increments either way, and
        this method never raises ``TaskProtectionError``.
        """
        async with self._lock:
            was_idle = self._live_sessions == 0
            self._live_sessions += 1
            if was_idle or not self._protected:
                await self._acquire_protection()

    async def session_ended(self) -> None:
        """Register a session end, releasing protection on the last one.

        Decrements the live-session count. When the count reaches 0 the
        refresh loop is cancelled and protection is released immediately
        under the bounded retry policy — synchronous with this call, so
        well within the 60-second bound (Req 10.4, 19.7). Release
        exhaustion is logged and left to self-heal via the protection
        expiry; readiness is deliberately not degraded, because the 503
        contract covers acquire only (Req 10.7). An end without a
        matching start is logged and ignored. This method never raises
        ``TaskProtectionError``.
        """
        async with self._lock:
            if self._live_sessions == 0:
                self._logger.warning(
                    "session_ended without a matching session_started; ignored",
                    extra={"event": SESSION_UNDERFLOW_EVENT},
                )
                return
            self._live_sessions -= 1
            if self._live_sessions > 0:
                return
            await self._stop_refresh_task()
            try:
                await retry_async(
                    self._protection.release,
                    retryable=TaskProtectionError,
                    retries=self._retries,
                    sleep=self._sleep,
                    logger=self._logger,
                    name="task_protection.release",
                )
            except TaskProtectionError:
                self._logger.exception(
                    "Task protection release exhausted retries; protection"
                    " will lapse at its expiry (readiness unchanged)",
                    extra={
                        "event": RELEASE_FAILED_EVENT,
                        "expiry_minutes": self._expiry_minutes,
                    },
                )
            else:
                self._protected = False
                self._logger.info(
                    "Task scale-in protection released; task is scale-in"
                    " eligible",
                    extra={"event": RELEASED_EVENT},
                )

    async def _acquire_protection(self) -> None:
        """Acquire or refresh protection and settle the manager state.

        Runs the port acquire under the bounded retry policy while the
        caller holds the transition lock. On success marks the task
        protected, restores readiness if it was degraded, and ensures the
        rolling refresh loop is running (a no-op when called from the
        loop itself). On exhaustion degrades readiness (Req 10.7) and
        leaves the protected flag unchanged. Never raises
        ``TaskProtectionError``.
        """
        try:
            await retry_async(
                partial(self._protection.acquire, self._expiry_minutes),
                retryable=TaskProtectionError,
                retries=self._retries,
                sleep=self._sleep,
                logger=self._logger,
                name="task_protection.acquire",
            )
        except TaskProtectionError:
            self._ready = False
            self._logger.exception(
                "Task protection acquire exhausted retries; readiness"
                " degraded until protection is confirmed",
                extra={
                    "event": READINESS_DEGRADED_EVENT,
                    "live_sessions": self._live_sessions,
                },
            )
        else:
            self._protected = True
            if not self._ready:
                self._ready = True
                self._logger.info(
                    "Task protection confirmed; readiness restored",
                    extra={
                        "event": READINESS_RESTORED_EVENT,
                        "live_sessions": self._live_sessions,
                    },
                )
            self._logger.info(
                "Task scale-in protection acquired",
                extra={
                    "event": ACQUIRED_EVENT,
                    "live_sessions": self._live_sessions,
                    "expiry_minutes": self._expiry_minutes,
                },
            )
            self._start_refresh_task()

    def _start_refresh_task(self) -> None:
        """Start the rolling refresh loop unless one is already running.

        Called after every confirmed acquire while the caller holds the
        transition lock. When invoked from within the refresh loop the
        stored task is the running current task, so nothing is started.
        """
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(),
                name="task-protection-refresh",
            )

    async def _stop_refresh_task(self) -> None:
        """Cancel the refresh loop and wait for it to finish.

        Called under the transition lock before releasing protection.
        ``CancelledError`` is suppressed only here, at the cancellation
        site; a loop blocked on the transition lock is woken by the
        cancellation, so this never deadlocks.
        """
        task = self._refresh_task
        self._refresh_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _refresh_loop(self) -> None:
        """Refresh the protection expiry on a rolling cadence (Req 10.3).

        Sleeps ``refresh_interval_seconds`` outside the lock, then — if
        the task is still protected with live sessions — re-acquires
        protection to reset its expiry. A refresh exhaustion degrades
        readiness inside :meth:`_acquire_protection`, and the loop keeps
        running so the next interval re-attempt can restore it; the loop
        exits once protection or the last session is gone, and is
        cancelled by :meth:`_stop_refresh_task` on the way to a release.
        """
        while True:
            await self._sleep(self._refresh_interval_seconds)
            async with self._lock:
                if not self._protected or self._live_sessions == 0:
                    return
                await self._acquire_protection()
