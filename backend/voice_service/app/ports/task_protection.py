"""Abstract interface to ECS task scale-in protection (Req 10.3, 10.4).

``TaskProtectionPort`` is the port boundary (Req 17.6) between the task
protection manager and the ECS agent. The implementing adapter
(``adapters.ecs_task_protection``) issues
``PUT $ECS_AGENT_URI/task-protection/v1/state`` for the task it runs in;
test suites substitute the deterministic in-memory
``FakeTaskProtection``.

The acquire/release policy is owned by the caller (the protection
manager and its live-session registry), not the port: protection is
acquired when the task's live Voice_Session count goes 0→1, its expiry
is refreshed on a rolling basis for long sessions via repeated
:meth:`TaskProtectionPort.acquire` calls, and it is released within 60
seconds of the count reaching 0 (Req 10.3, 10.4, 19.6, 19.7). Failures
raise ``TaskProtectionError``; the caller retries up to 3 times under
the shared bounded retry policy and, on exhaustion, flips the
``/healthz`` readiness flag to 503 until protection is confirmed
(Req 10.7). This module imports no SDK (Req 17.6).
"""

from abc import ABC, abstractmethod

__all__ = ["TaskProtectionPort"]


class TaskProtectionPort(ABC):
    """Scale-in protection of the current ECS task, as the manager sees it.

    Abstract base class over the two protection state changes: enabling
    protection with an expiry (also used to refresh it) and disabling
    it. Instances address the task the service runs in; counting live
    sessions and deciding when to acquire, refresh, or release is the
    protection manager's responsibility.
    """

    @abstractmethod
    async def acquire(self, expiry_minutes: int) -> None:
        """Enable (or refresh) scale-in protection for this task.

        Marks the task protected so scale-in never terminates it while
        it hosts live Voice_Sessions (Req 10.3, 19.6). Calling acquire
        on an already-protected task resets the expiry — the rolling
        refresh for sessions that outlive one expiry window.

        Args:
            expiry_minutes: Protection expiry in minutes; the protection
                lapses automatically if not refreshed or released before
                it elapses, bounding the impact of a missed release.

        Raises:
            TaskProtectionError: If the protection state change fails;
                the caller retries and degrades readiness on exhaustion
                (Req 10.7).
        """

    @abstractmethod
    async def release(self) -> None:
        """Disable scale-in protection for this task.

        Makes the task eligible for termination by subsequent scale-in
        actions; called within 60 seconds of the task's live-session
        count reaching zero (Req 10.4, 19.7). Releasing an unprotected
        task is a no-op.

        Raises:
            TaskProtectionError: If the protection state change fails;
                the caller retries under the shared bounded retry policy
                (Req 10.7).
        """
