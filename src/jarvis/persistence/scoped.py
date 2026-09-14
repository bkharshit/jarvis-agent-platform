"""Tenant-scoped repo views (S2, ADR 0009 §4) — authorization at the
repository boundary.

Each view binds one `tenant_id` and delegates to the underlying Sql repo,
which scopes the actual WHERE clauses (never post-filters). The views
structurally implement the `ports/` repository Protocols, so routes
consume them exactly where they consumed the raw repos — the ports are
untouched, nothing inward learns about tenants.

Scoping semantics:
- agents: reads include platform-shared rows (tenant_id NULL); writes
  stamp the binding tenant. Shared rows stay shared on update — an S2
  policy, to be tightened if shared agents ever become read-only.
- executions/conversations: strictly tenant-scoped — a foreign id resolves
  to None, which routes map to 404 (no existence leak).
- nested reads (messages/tool executions/events) delegate unscoped: routes
  reach them only after the scoped parent-run lookup succeeded, so
  authorization derives from the parent (ADR 0009 §5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import ExecutionStatus, RunResult
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolResult
from jarvis.domain.workflow import WorkflowDefinition, WorkflowVersion
from jarvis.persistence.repositories import (
    SqlAgentRepo,
    SqlConversationRepo,
    SqlExecutionRepo,
    SqlWorkflowRepo,
)
from jarvis.ports.queue import RunQueueMessage


class TenantScopedWorkflows:
    """WorkflowRepo over one tenant's workflows (+ platform-shared ones)."""

    def __init__(self, inner: SqlWorkflowRepo, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    async def create(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        return await self._inner.create(definition, tenant_id=self._tenant_id)

    async def get(self, workflow_id: str) -> WorkflowDefinition | None:
        return await self._inner.get(workflow_id, tenant_id=self._tenant_id)

    async def get_by_name(self, name: str) -> WorkflowDefinition | None:
        return await self._inner.get_by_name(name, tenant_id=self._tenant_id)

    async def list_workflows(self, limit: int = 50, offset: int = 0) -> list[WorkflowDefinition]:
        return await self._inner.list_workflows(
            limit=limit, offset=offset, tenant_id=self._tenant_id
        )

    async def update_and_publish(
        self, definition: WorkflowDefinition, label: str = ""
    ) -> WorkflowVersion:
        return await self._inner.update_and_publish(definition, label, tenant_id=self._tenant_id)

    async def get_version(self, workflow_id: str, version: int) -> WorkflowVersion | None:
        return await self._inner.get_version(workflow_id, version, tenant_id=self._tenant_id)

    async def latest_version(self, workflow_id: str) -> WorkflowVersion | None:
        return await self._inner.latest_version(workflow_id, tenant_id=self._tenant_id)

    async def list_versions(self, workflow_id: str) -> list[WorkflowVersion]:
        return await self._inner.list_versions(workflow_id, tenant_id=self._tenant_id)

    async def delete(self, workflow_id: str) -> bool:
        return await self._inner.delete(workflow_id, tenant_id=self._tenant_id)

    async def has_executions(self, workflow_id: str) -> bool:
        return await self._inner.has_executions(workflow_id)


class TenantScopedAgents:
    """AgentRepo over one tenant's agents (+ platform-shared ones)."""

    def __init__(self, inner: SqlAgentRepo, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    async def create(self, definition: AgentDefinition) -> AgentDefinition:
        return await self._inner.create(definition, tenant_id=self._tenant_id)

    async def get(self, agent_id: str) -> AgentDefinition | None:
        return await self._inner.get(agent_id, tenant_id=self._tenant_id)

    async def get_by_name(self, name: str) -> AgentDefinition | None:
        return await self._inner.get_by_name(name, tenant_id=self._tenant_id)

    async def list_agents(self, limit: int = 50, offset: int = 0) -> list[AgentDefinition]:
        return await self._inner.list_agents(limit=limit, offset=offset, tenant_id=self._tenant_id)

    async def update_and_publish(
        self, definition: AgentDefinition, label: str = ""
    ) -> AgentVersion:
        return await self._inner.update_and_publish(definition, label, tenant_id=self._tenant_id)

    async def get_version(self, agent_id: str, version: int) -> AgentVersion | None:
        return await self._inner.get_version(agent_id, version, tenant_id=self._tenant_id)

    async def latest_version(self, agent_id: str) -> AgentVersion | None:
        return await self._inner.latest_version(agent_id, tenant_id=self._tenant_id)

    async def list_versions(self, agent_id: str) -> list[AgentVersion]:
        return await self._inner.list_versions(agent_id, tenant_id=self._tenant_id)

    async def delete(self, agent_id: str) -> bool:
        return await self._inner.delete(agent_id, tenant_id=self._tenant_id)

    async def has_executions(self, agent_id: str) -> bool:
        return await self._inner.has_executions(agent_id)


class TenantScopedExecutions:
    """ExecutionRepo over one tenant's runs; nested reads ride the scoped
    parent lookup the route already performed."""

    def __init__(self, inner: SqlExecutionRepo, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    async def create_queued_run(self, result: RunResult, message: RunQueueMessage) -> None:
        if result.tenant_id is None:
            result = result.model_copy(update={"tenant_id": self._tenant_id})
        await self._inner.create_queued_run(result, message)

    async def create_run(self, result: RunResult) -> None:
        if result.tenant_id is None:
            result = result.model_copy(update={"tenant_id": self._tenant_id})
        await self._inner.create_run(result)

    async def finish_run(self, result: RunResult) -> None:
        await self._inner.finish_run(result)

    async def get(self, run_id: str) -> RunResult | None:
        return await self._inner.get(run_id, tenant_id=self._tenant_id)

    async def list_runs(
        self,
        agent_id: str | None = None,
        status: ExecutionStatus | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunResult]:
        return await self._inner.list_runs(
            agent_id=agent_id,
            status=status,
            session_id=session_id,
            limit=limit,
            offset=offset,
            tenant_id=self._tenant_id,
        )

    # nested reads — authorized by the caller's scoped `get` above

    async def save_message(self, run_id: str, message: Message) -> None:
        await self._inner.save_message(run_id, message)

    async def list_messages(self, run_id: str) -> list[Message]:
        return await self._inner.list_messages(run_id)

    async def save_tool_execution(
        self, run_id: str, result: ToolResult, arguments: dict[str, object]
    ) -> None:
        await self._inner.save_tool_execution(run_id, result, arguments)

    async def list_tool_executions(self, run_id: str) -> list[ToolResult]:
        return await self._inner.list_tool_executions(run_id)

    async def append_event(self, event: ExecutionEvent) -> int:
        return await self._inner.append_event(event)

    async def latest_event(self, run_id: str) -> tuple[int, ExecutionEvent] | None:
        """Nested read — the resume route needs the pause frame's cursor to
        attach its stream after it (S10)."""
        return await self._inner.latest_event(run_id)

    async def next_event_sequence(self, run_id: str) -> int:
        """Nested read — the cancel route's awaiting_input branch appends the
        terminal at the run's next sequence (S10, ADR 0010 §6)."""
        return await self._inner.next_event_sequence(run_id)

    def list_events(self, run_id: str, after: int | None = None) -> AsyncIterator[ExecutionEvent]:
        return self._inner.list_events(run_id, after)

    def replay_with_cursor(
        self, run_id: str, after: int | None = None
    ) -> AsyncIterator[tuple[int, ExecutionEvent]]:
        return self._inner.replay_with_cursor(run_id, after)


class TenantScopedConversations:
    """ConversationRepo over one tenant's conversations."""

    def __init__(self, inner: SqlConversationRepo, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    async def get_or_create(self, agent_id: str, session_id: str) -> str:
        return await self._inner.get_or_create(agent_id, session_id, tenant_id=self._tenant_id)

    async def find(self, agent_id: str, session_id: str) -> str | None:
        return await self._inner.find(agent_id, session_id, tenant_id=self._tenant_id)

    async def append_message(
        self, conversation_id: str, message: Message, run_id: str | None = None
    ) -> int:
        return await self._inner.append_message(conversation_id, message, run_id)

    async def history(self, conversation_id: str, limit: int | None = None) -> list[Message]:
        return await self._inner.history(conversation_id, limit, tenant_id=self._tenant_id)


__all__ = [
    "TenantScopedAgents",
    "TenantScopedConversations",
    "TenantScopedExecutions",
    "TenantScopedWorkflows",
]
