"""Abstract interface to the AWS DevOps Agent chat API.

``DevOpsAgentPort`` is the port boundary (Req 17.6) between the tool
router and the DevOps_Agent service: :meth:`DevOpsAgentPort.create_chat`
stands for ``aidevops:CreateChat`` (Req 3.2) and
:meth:`DevOpsAgentPort.send_message` for ``aidevops:SendMessage`` with
its streamed response (Req 3.3). The implementing adapter
(``adapters.devops_agent_client``) isolates the blocking boto3
``devops-agent`` client behind async execution; test suites substitute
the deterministic in-memory ``FakeDevOpsAgent``.

Chat lifecycle is owned by the caller (the tool router), not the port:
one chat per Voice_Session, created on the first ``ask_devops_agent``
invocation, persisted in the Session_Store, and reused for every
subsequent request (Req 3.2, 3.9) — recreated and re-persisted when the
mapping is missing (Req 3.10). Execution scoping follows the session's
origin: scoped iff the session carries an executionId (Req 3.5, 3.6).

The 60-second streaming budget is likewise enforced by the caller: the
tool router wraps consumption of :meth:`DevOpsAgentPort.send_message` in
``asyncio.timeout(60)`` and converts expiry into ``AgentTimeoutError``
(Req 3.8). Implementations raise ``AgentRequestError`` when an agent API
call itself fails (Req 3.7). This module imports no SDK (Req 17.6).
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

__all__ = ["DevOpsAgentPort"]


class DevOpsAgentPort(ABC):
    """The DevOps_Agent chat API, as the tool router sees it.

    Abstract base class over the two agent operations the portal uses:
    creating a chat (optionally scoped to a DevOps_Agent execution) and
    sending a message whose answer streams back in chunks. Stateless
    with respect to sessions — chat-to-session mapping and reuse live in
    the Session_Store and the tool router (Req 3.2, 3.9, 3.10).
    """

    @abstractmethod
    async def create_chat(self, execution_id: str | None) -> str:
        """Create a new DevOps_Agent chat and return its identifier.

        Stands for ``aidevops:CreateChat`` (Req 3.2). The chat is scoped
        to a DevOps_Agent execution exactly when ``execution_id`` is
        provided: sessions opened from an Incident_Notification carrying
        an executionId pass it here (Req 3.5); all other sessions pass
        ``None`` for an unscoped chat (Req 3.6).

        Args:
            execution_id: DevOps_Agent execution to scope the chat to,
                or ``None`` to create the chat without execution
                scoping.

        Returns:
            The identifier of the created chat, which the caller
            persists in the Session_Store keyed by the Voice_Session
            (Req 3.2).

        Raises:
            AgentRequestError: If the ``CreateChat`` call fails
                (Req 3.7).
        """

    @abstractmethod
    def send_message(self, chat_id: str, text: str) -> AsyncIterator[str]:
        """Send one engineer request and stream the agent's answer.

        Stands for ``aidevops:SendMessage`` (Req 3.3). The returned
        iterator yields the streamed response chunks in arrival order;
        the caller accumulates them into one complete response text
        (``domain.transcript.ChunkAccumulator``) before returning the
        tool result, and bounds total consumption with the 60-second
        budget (``asyncio.timeout``), converting expiry into
        ``AgentTimeoutError`` (Req 3.8).

        Args:
            chat_id: Identifier of the chat to send the request on —
                the one persisted for the Voice_Session (Req 3.9).
            text: The engineer request as text, exactly as extracted
                from the ``ask_devops_agent`` tool input.

        Returns:
            An async iterator over the streamed response chunks in
            arrival order; chunks may be empty and are concatenated
            verbatim by the caller (Req 3.3).

        Raises:
            AgentRequestError: If the ``SendMessage`` call fails, raised
                on the call or by the iterator mid-stream (Req 3.7).
        """
