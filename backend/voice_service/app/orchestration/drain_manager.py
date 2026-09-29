"""Connection draining on ECS task termination (Req 10.5, 10.8).

Implements the design's drain_manager component: when the ECS task
receives a termination signal, the service stops accepting new
WebSocket connections while existing Voice_Sessions continue for up to
the drain period — :data:`DEFAULT_DRAIN_SECONDS` (120 s), mirroring the
task's ECS ``stopTimeout`` (Req 10.5). Any session still active when
the period expires receives the ``{"type": "session.terminating"}``
notice before its WebSocket closes (Req 10.8).

Responsibility is split across three collaborators:

- **This module owns the scheduling**: the draining flag, the registry
  of live sessions, and the watchdog that — once the drain period
  expires — fans termination out to the sessions still registered.
- **The session manager owns the wire work**: for every live
  connection it registers a :data:`TerminateCallback` that sends the
  serialized ``SessionTerminatingFrame(reason="drain_timeout")`` from
  ``app.protocol.ws_messages`` and closes that WebSocket, and it
  unregisters the session again on natural close — so only stragglers
  are ever terminated (Req 10.8).
- **The composition root (``app.main``) owns the SIGTERM wiring**: its
  signal handler schedules :meth:`DrainManager.begin_drain`, its
  ``/ws/voice`` upgrade path rejects new connections while
  :attr:`DrainManager.accepting_new` is false, and ``/healthz``
  reports 503 while :attr:`DrainManager.draining` is true, so the ALB
  stops routing new traffic to the draining task (Req 10.5).

All state lives on the event-loop thread and every mutation happens
between awaits, so the manager needs no locks. The module performs no
I/O of its own and imports no SDKs (Req 17.6); the injected ``sleep``
lets tests drive the watchdog with a fake clock.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from shared.exceptions import PortalError

from app.logging import bind_session

__all__ = [
    "DEFAULT_DRAIN_SECONDS",
    "DrainManager",
    "TerminateCallback",
]

DEFAULT_DRAIN_SECONDS: Final = 120.0
"""Drain period in seconds, mirroring the ECS task ``stopTimeout`` (Req 10.5)."""

type TerminateCallback = Callable[[], Awaitable[None]]
"""Per-session terminator registered by the session manager.

Awaited by the drain watchdog for each session still registered when
the drain period expires. The callback owns the send and close: it
delivers the ``session.terminating`` notice with reason
``drain_timeout`` and closes that session's WebSocket (Req 10.8). It
raises only ``PortalError``, ``ConnectionError``, or ``OSError``
subclasses; the watchdog logs such failures and continues with the
remaining sessions.
"""


class DrainManager:
    """Schedules connection draining for one ECS task (Req 10.5, 10.8).

    Tracks whether the task is draining and which Voice_Sessions are
    live. Before :meth:`begin_drain` is called, :attr:`accepting_new`
    is true and registrations simply accumulate. Once the task receives
    SIGTERM and :meth:`begin_drain` runs, new WebSocket upgrades are
    rejected via :attr:`accepting_new` while registered sessions
    continue undisturbed; after ``drain_seconds`` the watchdog invokes
    the :data:`TerminateCallback` of every session still registered, so
    each straggler is notified before its connection closes. Sessions
    that end naturally during the drain window deregister themselves
    and are never terminated.
    """

    def __init__(
        self,
        *,
        drain_seconds: float = DEFAULT_DRAIN_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize an idle manager that is accepting new connections.

        Args:
            drain_seconds: How long existing sessions may continue after
                :meth:`begin_drain` before the watchdog terminates the
                stragglers. Defaults to :data:`DEFAULT_DRAIN_SECONDS`
                (120 s), matching the ECS ``stopTimeout`` (Req 10.5).
            sleep: Awaitable delay function used by the watchdog;
                defaults to :func:`asyncio.sleep`. Tests inject an
                instant fake to drive drain expiry deterministically.
            logger: Logger for drain lifecycle events; defaults to this
                module's logger.
        """
        self._drain_seconds = drain_seconds
        self._sleep = sleep
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._draining = False
        self._registry: dict[str, TerminateCallback] = {}
        self._watchdog: asyncio.Task[None] | None = None

    @property
    def draining(self) -> bool:
        """Return whether the task has begun draining.

        Returns:
            ``True`` once :meth:`begin_drain` has run; ``/healthz``
            reports 503 while this is set (Req 10.5).
        """
        return self._draining

    @property
    def accepting_new(self) -> bool:
        """Return whether new WebSocket connections may be accepted.

        Returns:
            ``True`` while the task is not draining. The ``/ws/voice``
            upgrade path consults this and rejects upgrades once it
            turns false (Req 10.5).
        """
        return not self._draining

    def register(self, session_id: str, terminate: TerminateCallback) -> None:
        """Register the terminator for one live Voice_Session.

        Called by the session manager for every connection it accepts,
        including one whose upgrade raced the termination signal — a
        session admitted mid-drain still receives its notice at expiry.
        Re-registering a session identifier replaces the stored
        callback, matching the one-live-connection-per-session model.

        Args:
            session_id: Identifier of the live Voice_Session.
            terminate: Callback that sends that session's
                ``session.terminating`` notice and closes its WebSocket
                when the drain period expires (Req 10.8).
        """
        self._registry[session_id] = terminate

    def unregister(self, session_id: str) -> None:
        """Deregister a session that no longer needs terminating.

        Called on natural session close, so only sessions still active
        at drain expiry are terminated (Req 10.8). Idempotent: unknown
        or already-removed identifiers are ignored, letting every
        close path deregister unconditionally.

        Args:
            session_id: Identifier of the Voice_Session to remove.
        """
        self._registry.pop(session_id, None)

    async def begin_drain(self) -> None:
        """Stop accepting new connections and start the drain watchdog.

        Invoked by the composition root's SIGTERM handler. Flips
        :attr:`accepting_new` to false (Req 10.5), logs the drain
        start, spawns the watchdog that terminates the sessions still
        registered after ``drain_seconds`` (Req 10.8), and returns
        immediately — existing sessions continue undisturbed while the
        watchdog sleeps. Idempotent: repeated signals leave the single
        running watchdog untouched.
        """
        if self._draining:
            return
        self._draining = True
        self._logger.info(
            "Drain started: rejecting new WebSocket connections",
            extra={
                "drain_seconds": self._drain_seconds,
                "live_sessions": len(self._registry),
            },
        )
        self._watchdog = asyncio.create_task(
            self._terminate_stragglers(), name="drain-watchdog"
        )

    async def wait_for_watchdog(self) -> None:
        """Wait until the drain watchdog has finished its fan-out.

        Lets the shutdown sequence — and tests — block until every
        straggler has been offered its termination notice. Safe to call
        at any time: a no-op when :meth:`begin_drain` has not run.
        """
        if self._watchdog is not None:
            await self._watchdog

    async def _terminate_stragglers(self) -> None:
        """Sleep out the drain period, then terminate remaining sessions.

        Snapshots the registry after the sleep so sessions that closed
        naturally during the drain window are untouched, then awaits
        each straggler's :data:`TerminateCallback` so it is notified
        before its WebSocket closes (Req 10.8). Callback failures are
        logged per session and never block the remaining notices.
        """
        await self._sleep(self._drain_seconds)
        stragglers = list(self._registry.items())
        self._logger.info(
            "Drain period expired; terminating still-active sessions",
            extra={"still_active_sessions": len(stragglers)},
        )
        for session_id, terminate in stragglers:
            if session_id not in self._registry:
                # Closed naturally (and deregistered) while an earlier
                # straggler was being notified; nothing left to terminate.
                continue
            with bind_session(session_id):
                try:
                    await terminate()
                except (PortalError, ConnectionError, OSError):
                    # Terminate-callback boundary: ConnectionError is an
                    # OSError subclass, named anyway because socket-level
                    # failures are the expected case when notifying a dying
                    # connection. One session's failure must never block the
                    # notices owed to the remaining sessions (Req 10.8).
                    self._logger.exception(
                        "Failed to deliver the session.terminating notice"
                    )
