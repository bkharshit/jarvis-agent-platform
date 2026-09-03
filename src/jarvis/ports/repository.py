"""Repository protocols — Protocols ONLY.

The Pydantic ↔ JSONB serialization boundary lives at the implementation
edge (ADR 0002). `update_and_publish` appends a new immutable AgentVersion
and repoints `agents.current_version` — history is never rewritten.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import ExecutionStatus, RunResult
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolResult


class AgentRepo(Protocol):
    async def create(self, definition: AgentDefinition) -> AgentDefinition:
        """Create the agent and publish its first version."""
        ...

    async def get(self, agent_id: str) -> AgentDefinition | None: ...

    async def get_by_name(self, name: str) -> AgentDefinition | None: ...

    async def list_agents(self, limit: int = 50, offset: int = 0) -> list[AgentDefinition]: ...

    async def update_and_publish(
        self, definition: AgentDefinition, label: str = ""
    ) -> AgentVersion:
        """Update mutable fields and append a new immutable snapshot."""
        ...

    async def get_version(self, agent_id: str, version: int) -> AgentVersion | None: ...

    async def latest_version(self, agent_id: str) -> AgentVersion | None: ...

    async def list_versions(self, agent_id: str) -> list[AgentVersion]: ...

    async def delete(self, agent_id: str) -> bool:
        """Returns False if the agent has executions (caller maps to 409)."""
        ...

    async def has_executions(self, agent_id: str) -> bool: ...


class ExecutionRepo(Protocol):
    async def create_run(self, result: RunResult) -> None: ...

    async def finish_run(self, result: RunResult) -> None: ...

    async def get(self, run_id: str) -> RunResult | None: ...

    async def list_runs(
        self,
        agent_id: str | None = None,
        status: ExecutionStatus | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunResult]: ...

    async def save_message(self, run_id: str, message: Message) -> None: ...

    async def list_messages(self, run_id: str) -> list[Message]: ...

    async def save_tool_execution(
        self, run_id: str, result: ToolResult, arguments: dict[str, object]
    ) -> None: ...

    async def list_tool_executions(self, run_id: str) -> list[ToolResult]: ...

    async def append_event(self, event: ExecutionEvent) -> int:
        """Persist with the sink-assigned per-run sequence; return the
        global cursor (SSE Last-Event-ID)."""
        ...

    async def list_events(
        self, run_id: str, after: int | None = None
    ) -> AsyncIterator[ExecutionEvent]: ...


class ConversationRepo(Protocol):
    async def get_or_create(self, agent_id: str, session_id: str) -> str: ...

    async def append_message(
        self, conversation_id: str, message: Message, run_id: str | None = None
    ) -> int:
        """Returns the message's conversation sequence."""
        ...

    async def history(self, conversation_id: str, limit: int | None = None) -> list[Message]: ...


__all__ = ["AgentRepo", "ConversationRepo", "ExecutionRepo"]
