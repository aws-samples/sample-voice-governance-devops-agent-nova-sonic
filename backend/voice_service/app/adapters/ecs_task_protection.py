"""ECS task scale-in protection adapter over the task-local agent endpoint.

Concrete ``TaskProtectionPort`` implementation
(``ports.task_protection.TaskProtectionPort``) that sets and clears
scale-in protection for the ECS task the service runs in by calling the
ECS agent's task protection endpoint —
``PUT $ECS_AGENT_URI/task-protection/v1/state`` (design research
finding 5, Req 10.3). Acquiring sends ``{"ProtectionEnabled": true,
"ExpiresInMinutes": <expiry>}``, which doubles as the rolling expiry
refresh for long sessions (Req 10.4); releasing sends
``{"ProtectionEnabled": false}``.

**Endpoint injection.** ``ECS_AGENT_URI`` is injected into the container
environment by the ECS agent itself at task startup; it exists only
inside a running ECS task and is therefore not part of the validated
configuration manifest owned by ``app.config``. The composition root
(``app.main``) reads the variable at wiring time and passes the base URI
in, keeping this adapter environment-free and fully injectable. The URI
addresses the task-scoped agent endpoint on a link-local address (it
carries its own path component, which plain string concatenation
preserves), so traffic never leaves the task's network namespace.

**Confirmation semantics.** A state change counts as confirmed only when
the agent answers 2xx with a JSON body that carries the ``protection``
object and no ``failure``/``error`` marker — the agent reports per-task
failures such as ``TASK_NOT_VALID`` inside a 200 body, mirroring the
``ecs:UpdateTaskProtection`` API. Anything else — non-2xx status, a
failure marker, an unreadable body, a connection error, or a timeout —
raises ``TaskProtectionError`` naming the attempted transition, and the
protection manager retries under the shared bounded retry policy before
degrading ``/healthz`` readiness (Req 10.7). Response bodies are never
logged or embedded in exception messages; only the fact that the
transition failed leaves this module.
"""

from typing import Final, Literal

import httpx

from app.exceptions import TaskProtectionError
from app.ports.task_protection import TaskProtectionPort

__all__ = ["EcsTaskProtectionAdapter"]

# Wire vocabulary of the ECS agent's task protection endpoint.
_STATE_PATH: Final = "/task-protection/v1/state"
_PROTECTION_ENABLED_KEY: Final = "ProtectionEnabled"
_EXPIRES_IN_MINUTES_KEY: Final = "ExpiresInMinutes"
_PROTECTION_KEY: Final = "protection"
_FAILURE_MARKER_KEYS: Final = ("failure", "error")

# The specific exception classes one request can raise: httpx.HTTPError
# covers transport failures and timeouts, httpx.InvalidURL a malformed
# agent URI. Never a bare Exception (Req 17.5).
_HTTP_ERRORS: Final = (httpx.HTTPError, httpx.InvalidURL)


def _confirms_state_change(response: httpx.Response) -> bool:
    """Return whether an agent response confirms the protection change.

    Applies the lenient confirmation rule from the module docstring: the
    status must be 2xx and the JSON body must carry the ``protection``
    object without any ``failure``/``error`` marker. A body that is not
    valid JSON, not an object, or missing the ``protection`` key does
    not confirm the change — protection state must never be assumed.

    Args:
        response: The agent's response to one state-change request.

    Returns:
        ``True`` only when the response confirms the state change per
        the rule above; ``False`` for every other response shape.
    """
    if not response.is_success:
        return False
    try:
        body: object = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    if any(body.get(key) for key in _FAILURE_MARKER_KEYS):
        return False
    return _PROTECTION_KEY in body


class EcsTaskProtectionAdapter(TaskProtectionPort):
    """Scale-in protection of the current ECS task via the agent endpoint.

    Concrete adapter behind ``TaskProtectionPort``: translates the
    port's acquire/refresh/release transitions into
    ``PUT $ECS_AGENT_URI/task-protection/v1/state`` calls against the
    task-local ECS agent (Req 10.3, 10.4). The acquire/release policy —
    when to protect, refresh, and release — is owned by the protection
    manager and its live-session registry; this adapter only performs
    and confirms the state changes.

    The agent base URI is injected by the composition root, which reads
    ``ECS_AGENT_URI`` from the container environment at wiring time; the
    adapter itself never touches the environment. Transport uses a
    lazily created ``httpx.AsyncClient`` bound to the configured
    per-request timeout; tests inject an ``httpx.MockTransport``-backed
    client instead. Call :meth:`aclose` at shutdown to release an owned
    client.
    """

    def __init__(
        self,
        ecs_agent_uri: str,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the adapter against one agent base URI.

        Args:
            ecs_agent_uri: Base URI of the task's ECS agent endpoint, as
                injected by the agent into ``ECS_AGENT_URI`` and read by
                the composition root (including its path component; a
                trailing slash is tolerated). The wiring validates
                presence — this adapter uses the value as given.
            timeout_seconds: Per-request timeout for the lazily created
                client. Each protection attempt is bounded by this so
                the caller's bounded retry policy (Req 10.7) is never
                stalled by a hung agent request. Ignored when ``client``
                is injected.
            client: Optional preconfigured ``httpx.AsyncClient`` to use
                instead of creating one — the seam tests use to supply
                an ``httpx.MockTransport``. An injected client's
                lifecycle belongs to its owner; :meth:`aclose` leaves it
                untouched.
        """
        self._state_url = ecs_agent_uri.rstrip("/") + _STATE_PATH
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def acquire(self, expiry_minutes: int) -> None:
        """Enable (or refresh) scale-in protection for this task.

        Sends ``{"ProtectionEnabled": true, "ExpiresInMinutes":
        <expiry_minutes>}`` to the agent's state endpoint. Acquiring an
        already-protected task resets the expiry, which is the rolling
        refresh for sessions that outlive one expiry window (Req 10.3,
        10.4).

        Args:
            expiry_minutes: Protection expiry in minutes; the protection
                lapses automatically if not refreshed or released before
                it elapses, bounding the impact of a missed release.

        Raises:
            TaskProtectionError: If the agent cannot be reached, the
                request times out, or the response does not confirm the
                state change; the caller retries and degrades readiness
                on exhaustion (Req 10.7).
        """
        await self._put_state(
            "acquire",
            {
                _PROTECTION_ENABLED_KEY: True,
                _EXPIRES_IN_MINUTES_KEY: expiry_minutes,
            },
        )

    async def release(self) -> None:
        """Disable scale-in protection for this task.

        Sends ``{"ProtectionEnabled": false}`` to the agent's state
        endpoint, making the task eligible for termination by subsequent
        scale-in actions (Req 10.4, 19.7). The agent confirms releasing
        an already-unprotected task, matching the port's no-op contract.

        Raises:
            TaskProtectionError: If the agent cannot be reached, the
                request times out, or the response does not confirm the
                state change; the caller retries under the shared
                bounded retry policy (Req 10.7).
        """
        await self._put_state("release", {_PROTECTION_ENABLED_KEY: False})

    async def aclose(self) -> None:
        """Close the lazily created HTTP client, if one exists.

        Closes only a client this adapter created itself; an injected
        client's lifecycle belongs to its owner and is left untouched.
        Safe to call repeatedly — a later state change recreates the
        owned client on demand.
        """
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _put_state(
        self,
        operation: Literal["acquire", "release"],
        payload: dict[str, object],
    ) -> None:
        """Issue one protection state change and verify the confirmation.

        Args:
            operation: The protection transition being attempted,
                ``"acquire"`` or ``"release"``; names the transition in
                the raised error.
            payload: JSON body for the agent endpoint in its
                ``ProtectionEnabled`` / ``ExpiresInMinutes`` wire shape.

        Raises:
            TaskProtectionError: If the request fails at the transport
                level (connection error, timeout, malformed endpoint) or
                the response does not confirm the state change.
        """
        client = self._http_client()
        try:
            response = await client.put(self._state_url, json=payload)
        except _HTTP_ERRORS as exc:
            raise TaskProtectionError(operation) from exc
        if not _confirms_state_change(response):
            raise TaskProtectionError(operation)

    def _http_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating the owned one on first use.

        Returns:
            The injected client when one was provided, otherwise a
            lazily created ``httpx.AsyncClient`` bound to the configured
            per-request timeout.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds)
            )
        return self._client
