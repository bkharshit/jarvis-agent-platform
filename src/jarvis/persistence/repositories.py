"""Repository implementations over Postgres (ADR 0002: typed
Pydantic-over-JSONB — domain objects serialize at this boundary, never
earlier).

`SqlExecutionRepo.append_event` returns the row's BIGSERIAL `cursor`; the
event sink uses that value as the SSE Last-Event-ID (ADR 0003).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import ColumnElement, bindparam, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import InstrumentedAttribute

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ConversationMemoryState,
    ScratchpadEntry,
)
from jarvis.domain.auth import (
    ApiKeyRecord,
    SessionRecord,
    StoredCredentialRecord,
    TenantRole,
    UserAccount,
)
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import ExecutionStatus, RunResult
from jarvis.domain.mcp import McpServer, McpServerConfig
from jarvis.domain.message import Message, Usage
from jarvis.domain.tools import ToolResult
from jarvis.domain.workflow import WorkflowDefinition, WorkflowVersion
from jarvis.persistence.models import (
    DEFAULT_TENANT,
    AgentExecutionRow,
    AgentRow,
    AgentVersionRow,
    ApiKeyRow,
    ConversationRow,
    CredentialRow,
    ExecutionEventRow,
    McpServerRow,
    MemoryScratchRow,
    MessageRow,
    RunCancelRow,
    RunQueueRow,
    SessionRow,
    TenantRow,
    ToolExecutionRow,
    UserRow,
    WorkflowRow,
    WorkflowVersionRow,
)
from jarvis.ports.queue import ResumeRequest, RunQueueMessage

_EVENT_ADAPTER: TypeAdapter[ExecutionEvent] = TypeAdapter(ExecutionEvent)
_MCP_CONFIG_ADAPTER: TypeAdapter[McpServerConfig] = TypeAdapter(McpServerConfig)


def create_sessionmaker(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(database_url)
    return async_sessionmaker(engine, expire_on_commit=False)


# --- serialization helpers (the only place domain meets JSONB) --------------


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


def _shared_visible(
    column: InstrumentedAttribute[str | None], tenant_id: str
) -> ColumnElement[bool]:
    """Rows the tenant may see: its own, or platform-shared (NULL). SQL
    `IN (t, NULL)` never matches NULL — this must be an explicit OR."""
    return (column == tenant_id) | column.is_(None)


class SqlAgentRepo:
    """Agents with append-only version history (snapshot-vs-reference)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def create(
        self, definition: AgentDefinition, *, tenant_id: str | None = None
    ) -> AgentDefinition:
        """`tenant_id=None` keeps the agent platform-shared (visible to and
        editable by every tenant) — the pre-S2 behavior."""
        version = AgentVersion(
            id=_uuid(),
            agent_id=definition.id,
            version=1,
            snapshot=definition,
            label="initial",
        )
        async with self._sessionmaker() as session:
            session.add(
                AgentRow(
                    id=definition.id,
                    name=definition.name,
                    description=definition.description,
                    current_version=1,
                    tenant_id=tenant_id,
                )
            )
            session.add(self._version_row(version))
            await session.commit()
        return definition

    async def get(self, agent_id: str, *, tenant_id: str | None = None) -> AgentDefinition | None:
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, agent_id, tenant_id)
            if row is None:
                return None
            snapshot = await self._snapshot_for(session, row)
        return AgentDefinition.model_validate(snapshot)

    async def get_by_name(
        self, name: str, *, tenant_id: str | None = None
    ) -> AgentDefinition | None:
        async with self._sessionmaker() as session:
            query = select(AgentRow).where(AgentRow.name == name)
            if tenant_id is not None:
                query = query.where(_shared_visible(AgentRow.tenant_id, tenant_id))
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                return None
            snapshot = await self._snapshot_for(session, row)
        return AgentDefinition.model_validate(snapshot)

    async def list_agents(
        self, limit: int = 50, offset: int = 0, *, tenant_id: str | None = None
    ) -> list[AgentDefinition]:
        async with self._sessionmaker() as session:
            query = select(AgentRow)
            if tenant_id is not None:
                query = query.where(_shared_visible(AgentRow.tenant_id, tenant_id))
            rows = (
                await session.execute(
                    query.order_by(AgentRow.created_at).limit(limit).offset(offset)
                )
            ).scalars()
            agents = []
            for row in rows:
                snapshot = await self._snapshot_for(session, row)
                agents.append(AgentDefinition.model_validate(snapshot))
        return agents

    async def update_and_publish(
        self, definition: AgentDefinition, label: str = "", *, tenant_id: str | None = None
    ) -> AgentVersion:
        """Update mutable fields and append a new immutable snapshot."""
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, definition.id, tenant_id)
            if row is None:
                raise LookupError(f"agent {definition.id} not found")
            next_version = row.current_version + 1
            row.name = definition.name
            row.description = definition.description
            row.current_version = next_version
            version = AgentVersion(
                id=_uuid(),
                agent_id=definition.id,
                version=next_version,
                snapshot=definition,
                label=label,
            )
            session.add(self._version_row(version))
            await session.commit()
        return version

    async def get_version(
        self, agent_id: str, version: int, *, tenant_id: str | None = None
    ) -> AgentVersion | None:
        async with self._sessionmaker() as session:
            row = await self._visible_version_row(session, agent_id, version, tenant_id)
            if row is None:
                return None
            return self._load_version(row)

    async def get_version_by_id(self, version_id: str) -> AgentVersion | None:
        """Load a frozen snapshot by its id — how a worker resolves the
        version pinned on a queue message."""
        async with self._sessionmaker() as session:
            row = await session.get(AgentVersionRow, version_id)
            if row is None:
                return None
            return self._load_version(row)

    async def latest_version(
        self, agent_id: str, *, tenant_id: str | None = None
    ) -> AgentVersion | None:
        async with self._sessionmaker() as session:
            query = (
                select(AgentVersionRow)
                .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
                .where(AgentVersionRow.agent_id == agent_id)
            )
            if tenant_id is not None:
                query = query.where(_shared_visible(AgentRow.tenant_id, tenant_id))
            row = (
                await session.execute(query.order_by(AgentVersionRow.version.desc()).limit(1))
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._load_version(row)

    async def list_versions(
        self, agent_id: str, *, tenant_id: str | None = None
    ) -> list[AgentVersion]:
        async with self._sessionmaker() as session:
            query = (
                select(AgentVersionRow)
                .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
                .where(AgentVersionRow.agent_id == agent_id)
            )
            if tenant_id is not None:
                query = query.where(_shared_visible(AgentRow.tenant_id, tenant_id))
            rows = (await session.execute(query.order_by(AgentVersionRow.version))).scalars()
            return [self._load_version(row) for row in rows]

    async def delete(self, agent_id: str, *, tenant_id: str | None = None) -> bool:
        """False if the agent has executions (caller maps to 409)."""
        if await self.has_executions(agent_id):
            return False
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, agent_id, tenant_id)
            if row is None:
                return False
            await session.delete(row)  # versions cascade
            await session.commit()
        return True

    async def has_executions(self, agent_id: str) -> bool:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(AgentExecutionRow.id)
                    .where(AgentExecutionRow.agent_id == agent_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return row is not None

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _visible_row(
        session: AsyncSession, agent_id: str, tenant_id: str | None
    ) -> AgentRow | None:
        """The agent row if visible to the tenant: owned, platform-shared
        (NULL), or — unscoped — simply existing."""
        if tenant_id is None:
            return await session.get(AgentRow, agent_id)
        query = select(AgentRow).where(
            AgentRow.id == agent_id, _shared_visible(AgentRow.tenant_id, tenant_id)
        )
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    async def _visible_version_row(
        session: AsyncSession, agent_id: str, version: int, tenant_id: str | None
    ) -> AgentVersionRow | None:
        query = (
            select(AgentVersionRow)
            .join(AgentRow, AgentRow.id == AgentVersionRow.agent_id)
            .where(AgentVersionRow.agent_id == agent_id, AgentVersionRow.version == version)
        )
        if tenant_id is not None:
            query = query.where(_shared_visible(AgentRow.tenant_id, tenant_id))
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    async def _snapshot_for(session: AsyncSession, row: AgentRow) -> dict[str, Any]:
        version_row = (
            await session.execute(
                select(AgentVersionRow.snapshot).where(
                    AgentVersionRow.agent_id == row.id,
                    AgentVersionRow.version == row.current_version,
                )
            )
        ).scalar_one()
        return dict(version_row)

    @staticmethod
    def _version_row(version: AgentVersion) -> AgentVersionRow:
        return AgentVersionRow(
            id=version.id,
            agent_id=version.agent_id,
            version=version.version,
            snapshot=version.snapshot.model_dump(mode="json"),
            label=version.label,
            created_at=version.created_at,
        )

    @staticmethod
    def _load_version(row: AgentVersionRow) -> AgentVersion:
        return AgentVersion(
            id=row.id,
            agent_id=row.agent_id,
            version=row.version,
            snapshot=AgentDefinition.model_validate(row.snapshot),
            label=row.label,
            created_at=row.created_at,
        )


class SqlMcpServerRepo:
    """Configured MCP servers (S4, ADR 0012) — the agents repo's tenancy
    pattern verbatim: `_shared_visible` reads, writes stamp the binding
    tenant, None = platform-shared (NULL tenant_id)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def create(self, server: McpServer, *, tenant_id: str | None = None) -> McpServer:
        async with self._sessionmaker() as session:
            session.add(
                McpServerRow(
                    id=server.id,
                    tenant_id=tenant_id,
                    name=server.name,
                    enabled=server.enabled,
                    config=server.config.model_dump(mode="json"),
                    created_at=server.created_at,
                    updated_at=server.updated_at,
                )
            )
            await session.commit()
        return server.model_copy(update={"tenant_id": tenant_id})

    async def get(self, server_id: str, *, tenant_id: str | None = None) -> McpServer | None:
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, server_id, tenant_id)
        return None if row is None else self._load(row)

    async def get_by_name(self, name: str, *, tenant_id: str | None = None) -> McpServer | None:
        async with self._sessionmaker() as session:
            query = select(McpServerRow).where(McpServerRow.name == name)
            if tenant_id is not None:
                query = query.where(_shared_visible(McpServerRow.tenant_id, tenant_id))
            # Tenant-owned rows shadow same-name shared rows: owned first.
            query = query.order_by(McpServerRow.tenant_id.is_(None)).limit(1)
            row = (await session.execute(query)).scalar_one_or_none()
        return None if row is None else self._load(row)

    async def list_servers(self, *, tenant_id: str | None = None) -> list[McpServer]:
        async with self._sessionmaker() as session:
            query = select(McpServerRow)
            if tenant_id is not None:
                query = query.where(_shared_visible(McpServerRow.tenant_id, tenant_id))
            rows = (
                await session.execute(query.order_by(McpServerRow.created_at, McpServerRow.id))
            ).scalars()
            servers = [self._load(row) for row in rows]
        return _shadow_shared(servers)

    async def update(self, server: McpServer, *, tenant_id: str | None = None) -> McpServer:
        """`enabled` and `config` only — the name is immutable (the join key
        from version snapshots), so any incoming name is ignored."""
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, server.id, tenant_id)
            if row is None:
                raise LookupError(f"mcp server {server.id} not found")
            row.enabled = server.enabled
            row.config = server.config.model_dump(mode="json")
            row.updated_at = _now()
            await session.commit()
            loaded = self._load(row)
        return loaded

    async def delete(self, server_id: str, *, tenant_id: str | None = None) -> bool:
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, server_id, tenant_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _visible_row(
        session: AsyncSession, server_id: str, tenant_id: str | None
    ) -> McpServerRow | None:
        """The server row if visible to the tenant: owned, platform-shared
        (NULL), or — unscoped — simply existing."""
        if tenant_id is None:
            return await session.get(McpServerRow, server_id)
        query = select(McpServerRow).where(
            McpServerRow.id == server_id, _shared_visible(McpServerRow.tenant_id, tenant_id)
        )
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    def _load(row: McpServerRow) -> McpServer:
        return McpServer(
            id=row.id,
            name=row.name,
            config=_MCP_CONFIG_ADAPTER.validate_python(row.config),
            enabled=row.enabled,
            tenant_id=row.tenant_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SqlWorkflowRepo:
    """Workflows with append-only version history (S6, ADR 0015) — the
    SqlAgentRepo shape mirrored one level up: pointer row + immutable
    JSONB snapshot, `_shared_visible` reads, writes stamp the binding
    tenant, None = platform-shared. Agent-node pins are frozen by the
    API's publish step (D42) — this repo stores the snapshot it is given."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def create(
        self, definition: WorkflowDefinition, *, tenant_id: str | None = None
    ) -> WorkflowDefinition:
        version = WorkflowVersion(
            id=_uuid(),
            workflow_id=definition.id,
            version=1,
            snapshot=definition,
            label="initial",
        )
        async with self._sessionmaker() as session:
            session.add(
                WorkflowRow(
                    id=definition.id,
                    name=definition.name,
                    description=definition.description,
                    current_version=1,
                    tenant_id=tenant_id,
                )
            )
            session.add(self._version_row(version))
            await session.commit()
        return definition

    async def get(
        self, workflow_id: str, *, tenant_id: str | None = None
    ) -> WorkflowDefinition | None:
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, workflow_id, tenant_id)
            if row is None:
                return None
            snapshot = await self._snapshot_for(session, row)
        return WorkflowDefinition.model_validate(snapshot)

    async def get_by_name(
        self, name: str, *, tenant_id: str | None = None
    ) -> WorkflowDefinition | None:
        async with self._sessionmaker() as session:
            query = select(WorkflowRow).where(WorkflowRow.name == name)
            if tenant_id is not None:
                query = query.where(_shared_visible(WorkflowRow.tenant_id, tenant_id))
            # Tenant-owned rows shadow same-name shared rows (D37): owned first.
            query = query.order_by(WorkflowRow.tenant_id.is_(None)).limit(1)
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                return None
            snapshot = await self._snapshot_for(session, row)
        return WorkflowDefinition.model_validate(snapshot)

    async def list_workflows(
        self, limit: int = 50, offset: int = 0, *, tenant_id: str | None = None
    ) -> list[WorkflowDefinition]:
        async with self._sessionmaker() as session:
            query = select(WorkflowRow)
            if tenant_id is not None:
                query = query.where(_shared_visible(WorkflowRow.tenant_id, tenant_id))
            rows = (
                await session.execute(
                    query.order_by(WorkflowRow.created_at).limit(limit).offset(offset)
                )
            ).scalars()
            workflows = []
            for row in rows:
                snapshot = await self._snapshot_for(session, row)
                workflows.append(WorkflowDefinition.model_validate(snapshot))
        return workflows

    async def update_and_publish(
        self, definition: WorkflowDefinition, label: str = "", *, tenant_id: str | None = None
    ) -> WorkflowVersion:
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, definition.id, tenant_id)
            if row is None:
                raise LookupError(f"workflow {definition.id} not found")
            next_version = row.current_version + 1
            row.name = definition.name
            row.description = definition.description
            row.current_version = next_version
            version = WorkflowVersion(
                id=_uuid(),
                workflow_id=definition.id,
                version=next_version,
                snapshot=definition,
                label=label,
            )
            session.add(self._version_row(version))
            await session.commit()
        return version

    async def get_version(
        self, workflow_id: str, version: int, *, tenant_id: str | None = None
    ) -> WorkflowVersion | None:
        async with self._sessionmaker() as session:
            row = await self._visible_version_row(session, workflow_id, version, tenant_id)
            if row is None:
                return None
            return self._load_version(row)

    async def get_version_by_id(self, version_id: str) -> WorkflowVersion | None:
        """Load a frozen snapshot by its id — how the worker resolves the
        workflow version pinned on a queue message (D41)."""
        async with self._sessionmaker() as session:
            row = await session.get(WorkflowVersionRow, version_id)
            if row is None:
                return None
            return self._load_version(row)

    async def latest_version(
        self, workflow_id: str, *, tenant_id: str | None = None
    ) -> WorkflowVersion | None:
        async with self._sessionmaker() as session:
            query = (
                select(WorkflowVersionRow)
                .join(WorkflowRow, WorkflowRow.id == WorkflowVersionRow.workflow_id)
                .where(WorkflowVersionRow.workflow_id == workflow_id)
            )
            if tenant_id is not None:
                query = query.where(_shared_visible(WorkflowRow.tenant_id, tenant_id))
            row = (
                await session.execute(query.order_by(WorkflowVersionRow.version.desc()).limit(1))
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._load_version(row)

    async def list_versions(
        self, workflow_id: str, *, tenant_id: str | None = None
    ) -> list[WorkflowVersion]:
        async with self._sessionmaker() as session:
            query = (
                select(WorkflowVersionRow)
                .join(WorkflowRow, WorkflowRow.id == WorkflowVersionRow.workflow_id)
                .where(WorkflowVersionRow.workflow_id == workflow_id)
            )
            if tenant_id is not None:
                query = query.where(_shared_visible(WorkflowRow.tenant_id, tenant_id))
            rows = (await session.execute(query.order_by(WorkflowVersionRow.version))).scalars()
            return [self._load_version(row) for row in rows]

    async def delete(self, workflow_id: str, *, tenant_id: str | None = None) -> bool:
        """False if the workflow has executions (caller maps to 409) —
        workflow runs live in agent_executions keyed by the workflow id
        (D41), so the same guard pattern as agents applies."""
        if await self.has_executions(workflow_id):
            return False
        async with self._sessionmaker() as session:
            row = await self._visible_row(session, workflow_id, tenant_id)
            if row is None:
                return False
            await session.delete(row)  # versions cascade
            await session.commit()
        return True

    async def has_executions(self, workflow_id: str) -> bool:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(AgentExecutionRow.id)
                    .where(AgentExecutionRow.agent_id == workflow_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return row is not None

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _visible_row(
        session: AsyncSession, workflow_id: str, tenant_id: str | None
    ) -> WorkflowRow | None:
        if tenant_id is None:
            return await session.get(WorkflowRow, workflow_id)
        query = select(WorkflowRow).where(
            WorkflowRow.id == workflow_id, _shared_visible(WorkflowRow.tenant_id, tenant_id)
        )
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    async def _visible_version_row(
        session: AsyncSession, workflow_id: str, version: int, tenant_id: str | None
    ) -> WorkflowVersionRow | None:
        query = (
            select(WorkflowVersionRow)
            .join(WorkflowRow, WorkflowRow.id == WorkflowVersionRow.workflow_id)
            .where(
                WorkflowVersionRow.workflow_id == workflow_id,
                WorkflowVersionRow.version == version,
            )
        )
        if tenant_id is not None:
            query = query.where(_shared_visible(WorkflowRow.tenant_id, tenant_id))
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    async def _snapshot_for(session: AsyncSession, row: WorkflowRow) -> dict[str, Any]:
        version_row = (
            await session.execute(
                select(WorkflowVersionRow.snapshot).where(
                    WorkflowVersionRow.workflow_id == row.id,
                    WorkflowVersionRow.version == row.current_version,
                )
            )
        ).scalar_one()
        return dict(version_row)

    @staticmethod
    def _version_row(version: WorkflowVersion) -> WorkflowVersionRow:
        return WorkflowVersionRow(
            id=version.id,
            workflow_id=version.workflow_id,
            version=version.version,
            snapshot=version.snapshot.model_dump(mode="json"),
            label=version.label,
            created_at=version.created_at,
        )

    @staticmethod
    def _load_version(row: WorkflowVersionRow) -> WorkflowVersion:
        return WorkflowVersion(
            id=row.id,
            workflow_id=row.workflow_id,
            version=row.version,
            snapshot=WorkflowDefinition.model_validate(dict(row.snapshot)),
            label=row.label,
            created_at=row.created_at,
        )


def _shadow_shared(servers: list[McpServer]) -> list[McpServer]:
    """Tenant-owned rows shadow same-name shared rows in listings (D37):
    a shared row is dropped when a tenant-owned row carries its name —
    owned rows themselves are always kept."""
    seen_owned: set[str] = set()
    for server in servers:
        if server.tenant_id is not None:
            seen_owned.add(server.name)
    return [s for s in servers if s.tenant_id is not None or s.name not in seen_owned]


class SqlExecutionRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def create_queued_run(self, result: RunResult, message: RunQueueMessage) -> None:
        """Execution row (`status='queued'`) + queue message in ONE
        transaction (ADR 0008 §2) — a message can never dangle without its
        row. The runtime flips the row to running when a worker claims it."""
        if result.status != "queued" or message.run_id != result.run_id:
            raise ValueError("create_queued_run requires a matching queued RunResult")
        async with self._sessionmaker() as session:
            session.add(self._run_row(result))
            session.add(RunQueueRow(run_id=message.run_id, payload=message.model_dump(mode="json")))
            await session.commit()

    async def mark_running(self, run_id: str, started_at: datetime) -> None:
        """queued → running when a worker claims the message (re-claims of a
        requeued run are a no-op — the row is already running). Accepts
        `awaiting_input` too (S10): the resume segment's claim flips the
        paused row back to running and clears the pause deadline."""
        async with self._sessionmaker() as session:
            await session.execute(
                update(AgentExecutionRow)
                .where(
                    AgentExecutionRow.id == run_id,
                    AgentExecutionRow.status.in_(("queued", "awaiting_input")),
                )
                .values(status="running", started_at=started_at, awaiting_until=None)
            )
            await session.commit()

    async def mark_awaiting_input(
        self, run_id: str, awaiting_until: datetime, *, total_usage: Usage | None = None
    ) -> None:
        """running → awaiting_input (S10, ADR 0010 §2): the loop paused. The
        deadline lands on the row for the sweeper's reaper; a stale guard
        (status == 'running') keeps a raced resume claim from pausing a row
        that already moved on. `total_usage` writes the chain's usage-so-far
        — the resume segment re-seeds its budget from the row."""
        values: dict[str, object] = {"status": "awaiting_input", "awaiting_until": awaiting_until}
        if total_usage is not None:
            values["total_usage"] = total_usage.model_dump(mode="json")
        async with self._sessionmaker() as session:
            await session.execute(
                update(AgentExecutionRow)
                .where(
                    AgentExecutionRow.id == run_id,
                    AgentExecutionRow.status == "running",
                )
                .values(**values)
            )
            await session.commit()

    async def expired_awaiting(self, now: datetime) -> list[str]:
        """Run_ids whose pause deadline has passed — the sweeper's
        pause-reaper input (S10, ADR 0010 §6)."""
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(AgentExecutionRow.id).where(
                        AgentExecutionRow.status == "awaiting_input",
                        AgentExecutionRow.awaiting_until < now,
                    )
                )
            ).scalars()
        return list(rows)

    async def count_events(self, run_id: str) -> int:
        async with self._sessionmaker() as session:
            return (
                await session.execute(
                    select(func.count())
                    .select_from(ExecutionEventRow)
                    .where(ExecutionEventRow.execution_id == run_id)
                )
            ).scalar_one()

    async def next_event_sequence(self, run_id: str) -> int:
        """The sequence a new event for this run must carry (the sweeper
        appends a terminal event at max+1 after a worker died)."""
        async with self._sessionmaker() as session:
            current = (
                await session.execute(
                    select(func.max(ExecutionEventRow.sequence)).where(
                        ExecutionEventRow.execution_id == run_id
                    )
                )
            ).scalar_one_or_none()
        return 0 if current is None else current + 1

    async def latest_event(self, run_id: str) -> tuple[int, ExecutionEvent] | None:
        """The run's highest-sequence event with its durable cursor."""
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ExecutionEventRow)
                    .where(ExecutionEventRow.execution_id == run_id)
                    .order_by(ExecutionEventRow.sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return row.cursor, _EVENT_ADAPTER.validate_python(row.payload)

    async def create_run(self, result: RunResult) -> None:
        """Ensure the run row exists and is RUNNING — insert it, or flip an
        existing `queued` row when a worker claims the run (S1: the API wrote
        the row at enqueue time; the runtime's write becomes the claim)."""
        row = self._run_row(result)
        async with self._sessionmaker() as session:
            await session.execute(
                pg_insert(AgentExecutionRow)
                .values(
                    id=row.id,
                    agent_id=row.agent_id,
                    agent_version_id=row.agent_version_id,
                    tenant_id=row.tenant_id,
                    session_id=row.session_id,
                    user_id=row.user_id,
                    trace_id=row.trace_id,
                    status=row.status,
                    input=row.input,
                    output=row.output,
                    error=row.error,
                    error_kind=row.error_kind,
                    total_usage=row.total_usage,
                    iterations=row.iterations,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                    event_cursor=row.event_cursor,
                    metadata_json=row.metadata_json,
                )
                .on_conflict_do_update(
                    index_elements=[AgentExecutionRow.id],
                    set_={"status": "running", "started_at": row.started_at},
                )
            )
            await session.commit()

    async def finish_run(self, result: RunResult) -> None:
        """Persist the terminal state; upserts in case no RUNNING row exists."""
        async with self._sessionmaker() as session:
            row = await session.get(AgentExecutionRow, result.run_id)
            if row is None:
                session.add(self._run_row(result))
            else:
                row.status = result.status
                row.output = result.final_message
                row.error = result.error
                row.error_kind = result.error_kind
                row.total_usage = result.total_usage.model_dump(mode="json")
                row.iterations = result.iterations
                row.started_at = result.started_at
                row.finished_at = result.finished_at or _now()
                row.event_cursor = result.event_cursor
                row.awaiting_until = None  # terminal clears a pause (S10)
            await session.commit()

    async def get(self, run_id: str, *, tenant_id: str | None = None) -> RunResult | None:
        async with self._sessionmaker() as session:
            if tenant_id is None:
                row = await session.get(AgentExecutionRow, run_id)
            else:
                row = (
                    await session.execute(
                        select(AgentExecutionRow).where(
                            AgentExecutionRow.id == run_id,
                            AgentExecutionRow.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
            if row is None:
                return None
            return self._load_run(row)

    async def list_runs(
        self,
        agent_id: str | None = None,
        status: ExecutionStatus | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        tenant_id: str | None = None,
    ) -> list[RunResult]:
        query = select(AgentExecutionRow)
        if tenant_id is not None:
            query = query.where(AgentExecutionRow.tenant_id == tenant_id)
        if agent_id is not None:
            query = query.where(AgentExecutionRow.agent_id == agent_id)
        if status is not None:
            query = query.where(AgentExecutionRow.status == status)
        if session_id is not None:
            query = query.where(AgentExecutionRow.session_id == session_id)
        query = query.order_by(AgentExecutionRow.created_at.desc()).limit(limit).offset(offset)
        async with self._sessionmaker() as session:
            rows = (await session.execute(query)).scalars()
            return [self._load_run(row) for row in rows]

    async def save_message(self, run_id: str, message: Message) -> None:
        """Append to the run transcript; `sequence` orders the transcript."""
        data = message.model_dump(mode="json")
        async with self._sessionmaker() as session:
            next_seq = await self._next_sequence(
                session, MessageRow.execution_id == run_id, MessageRow.sequence
            )
            session.add(
                MessageRow(
                    id=_uuid(),
                    execution_id=run_id,
                    role=data["role"],
                    content=data["content"],
                    tool_calls=data.get("tool_calls"),
                    tool_call_id=data.get("tool_call_id"),
                    name=data.get("name"),
                    sequence=next_seq,
                )
            )
            await session.commit()

    async def list_messages(self, run_id: str) -> list[Message]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.execution_id == run_id)
                    .order_by(MessageRow.sequence)
                )
            ).scalars()
            return [self._load_message(row) for row in rows]

    async def save_tool_execution(
        self, run_id: str, result: ToolResult, arguments: dict[str, object]
    ) -> None:
        async with self._sessionmaker() as session:
            session.add(
                ToolExecutionRow(
                    id=_uuid(),
                    execution_id=run_id,
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                    arguments=dict(arguments),
                    result={
                        "output": result.output,
                        "is_error": result.is_error,
                        "metadata": result.metadata,
                    },
                    is_error=result.is_error,
                    latency_ms=result.latency_ms,
                )
            )
            await session.commit()

    async def list_tool_executions(self, run_id: str) -> list[ToolResult]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(ToolExecutionRow)
                    .where(ToolExecutionRow.execution_id == run_id)
                    .order_by(ToolExecutionRow.created_at)
                )
            ).scalars()
            return [self._load_tool_result(row) for row in rows]

    async def append_event(self, event: ExecutionEvent) -> int:
        """Persist with the sink-assigned per-run sequence; return the global
        cursor (SSE Last-Event-ID)."""
        async with self._sessionmaker() as session:
            row = ExecutionEventRow(
                execution_id=event.run_id,
                event_type=event.type,
                sequence=event.sequence or 0,
                payload=event.model_dump(mode="json"),
            )
            session.add(row)
            await session.commit()
        return row.cursor

    async def list_events(
        self, run_id: str, after: int | None = None
    ) -> AsyncIterator[ExecutionEvent]:
        query = (
            select(ExecutionEventRow)
            .where(ExecutionEventRow.execution_id == run_id)
            .order_by(ExecutionEventRow.sequence)
        )
        if after is not None:
            query = query.where(ExecutionEventRow.sequence > after)
        async with self._sessionmaker() as session:
            rows = (await session.execute(query)).scalars()
            for row in rows:
                yield _EVENT_ADAPTER.validate_python(row.payload)

    async def replay_with_cursor(
        self, run_id: str, after: int | None = None
    ) -> AsyncIterator[tuple[int, ExecutionEvent]]:
        """Cursor-space replay: yields (durable global cursor, event) pairs
        after the given cursor — the SSE Last-Event-ID resume path for
        finished runs (ADR 0003; `list_events` above is per-run-sequence
        order for transcript replays)."""
        query = (
            select(ExecutionEventRow)
            .where(ExecutionEventRow.execution_id == run_id)
            .order_by(ExecutionEventRow.cursor)
        )
        if after is not None:
            query = query.where(ExecutionEventRow.cursor > after)
        async with self._sessionmaker() as session:
            rows = (await session.execute(query)).scalars()
            for row in rows:
                yield row.cursor, _EVENT_ADAPTER.validate_python(row.payload)

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _next_sequence(
        session: AsyncSession, where: ColumnElement[bool], column: InstrumentedAttribute[int | None]
    ) -> int:
        current = (
            await session.execute(select(func.max(column)).where(where))
        ).scalar_one_or_none()
        return 0 if current is None else current + 1

    @staticmethod
    def _run_row(result: RunResult) -> AgentExecutionRow:
        return AgentExecutionRow(
            id=result.run_id,
            agent_id=result.agent_id,
            agent_version_id=result.agent_version_id,
            # NULL would fall back to the column's 'default' — stamp the
            # principal's tenant explicitly (S2).
            tenant_id=result.tenant_id or DEFAULT_TENANT,
            session_id=result.session_id,
            trace_id=result.trace_id,
            status=result.status,
            input=result.input,
            output=result.final_message,
            error=result.error,
            error_kind=result.error_kind,
            total_usage=result.total_usage.model_dump(mode="json"),
            iterations=result.iterations,
            started_at=result.started_at,
            finished_at=result.finished_at,
            event_cursor=result.event_cursor,
            metadata_json=result.metadata,
        )

    @staticmethod
    def _load_run(row: AgentExecutionRow) -> RunResult:
        return RunResult(
            run_id=row.id,
            agent_id=row.agent_id,
            status=cast(ExecutionStatus, row.status),
            input=row.input,
            agent_version_id=row.agent_version_id,
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            trace_id=row.trace_id,
            final_message=row.output,
            total_usage=Usage.model_validate(row.total_usage),
            iterations=row.iterations,
            error=row.error,
            error_kind=row.error_kind,
            started_at=row.started_at,
            finished_at=row.finished_at,
            event_cursor=row.event_cursor,
            metadata=row.metadata_json or {},
        )

    @staticmethod
    def _load_message(row: MessageRow) -> Message:
        return Message.model_validate(
            {
                "role": row.role,
                "content": row.content,
                "tool_calls": row.tool_calls,
                "tool_call_id": row.tool_call_id,
                "name": row.name,
                "created_at": row.created_at,
            }
        )

    @staticmethod
    def _load_tool_result(row: ToolExecutionRow) -> ToolResult:
        result = row.result or {}
        return ToolResult(
            tool_call_id=row.tool_call_id,
            tool_name=row.tool_name,
            output=result.get("output", ""),
            is_error=row.is_error,
            latency_ms=row.latency_ms,
            metadata=result.get("metadata", {}),
        )


class SqlConversationRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def get_or_create(
        self, agent_id: str, session_id: str, *, tenant_id: str | None = None
    ) -> str:
        """`tenant_id=None` stamps the default tenant — the pre-S2 behavior;
        the runtime passes `ctx.tenant_id` once the worker threads it."""
        effective = tenant_id or DEFAULT_TENANT
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ConversationRow).where(
                        ConversationRow.agent_id == agent_id,
                        ConversationRow.session_id == session_id,
                        ConversationRow.tenant_id == effective,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return row.id
            row = ConversationRow(
                id=_uuid(), agent_id=agent_id, session_id=session_id, tenant_id=effective
            )
            session.add(row)
            await session.commit()
            return row.id

    async def find(
        self, agent_id: str, session_id: str, *, tenant_id: str | None = None
    ) -> str | None:
        """Look up without creating — the read-side pair of get_or_create."""
        async with self._sessionmaker() as session:
            query = select(ConversationRow.id).where(
                ConversationRow.agent_id == agent_id,
                ConversationRow.session_id == session_id,
            )
            if tenant_id is not None:
                query = query.where(ConversationRow.tenant_id == tenant_id)
            return (await session.execute(query)).scalar_one_or_none()

    async def append_message(
        self, conversation_id: str, message: Message, run_id: str | None = None
    ) -> int:
        """Returns the message's conversation sequence (gapless per
        conversation, UNIQUE-backed)."""
        data = message.model_dump(mode="json")
        async with self._sessionmaker() as session:
            next_seq = await SqlExecutionRepo._next_sequence(
                session, MessageRow.conversation_id == conversation_id, MessageRow.sequence
            )
            session.add(
                MessageRow(
                    id=_uuid(),
                    conversation_id=conversation_id,
                    execution_id=run_id,
                    role=data["role"],
                    content=data["content"],
                    tool_calls=data.get("tool_calls"),
                    tool_call_id=data.get("tool_call_id"),
                    name=data.get("name"),
                    sequence=next_seq,
                )
            )
            await session.commit()
        return next_seq

    async def history(
        self, conversation_id: str, limit: int | None = None, *, tenant_id: str | None = None
    ) -> list[Message]:
        query = select(MessageRow).where(MessageRow.conversation_id == conversation_id)
        if tenant_id is not None:
            # the conversation must belong to the tenant (parent-row check)
            query = query.where(
                MessageRow.conversation_id.in_(
                    select(ConversationRow.id).where(
                        ConversationRow.id == conversation_id,
                        ConversationRow.tenant_id == tenant_id,
                    )
                )
            )
        if limit is not None:
            # window from the END of the conversation (most recent N)
            query = query.order_by(MessageRow.sequence.desc()).limit(limit)
        else:
            query = query.order_by(MessageRow.sequence)
        async with self._sessionmaker() as session:
            rows = list((await session.execute(query)).scalars())
        if limit is not None:
            rows.reverse()
        return [SqlExecutionRepo._load_message(row) for row in rows]

    async def get_summary_state(
        self, conversation_id: str, *, tenant_id: str | None = None
    ) -> ConversationMemoryState | None:
        """S12 (D45): the rolling-summary state. None when the conversation
        does not exist (or belongs to another tenant, D29 — absent, not
        leaked)."""
        query = select(ConversationRow.summary, ConversationRow.summarized_count).where(
            ConversationRow.id == conversation_id
        )
        if tenant_id is not None:
            query = query.where(ConversationRow.tenant_id == tenant_id)
        async with self._sessionmaker() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            return None
        return ConversationMemoryState(summary=row.summary, summarized_count=row.summarized_count)

    async def save_summary(
        self,
        conversation_id: str,
        *,
        summary: str,
        summarized_count: int,
        tenant_id: str | None = None,
    ) -> None:
        """S12 (D45): persist the compaction result. A foreign tenant_id
        matches no row — the write is silently a no-op, same absence rule
        as every other scoped write (D29)."""
        query = update(ConversationRow).where(ConversationRow.id == conversation_id)
        if tenant_id is not None:
            query = query.where(ConversationRow.tenant_id == tenant_id)
        query = query.values(summary=summary, summarized_count=summarized_count)
        async with self._sessionmaker() as session:
            await session.execute(query)
            await session.commit()


class SqlScratchpadRepo:
    """Working-memory KV store (S12, ADR 0016 §3, D46) over `memory_scratch`.

    Tenant discipline (D29): a `tenant_id` filters reads and scopes writes —
    a foreign tenant's keys read as absent and its rows are never touched.
    Writes stamp the default tenant when none is supplied (the pre-S2
    behavior every repo shares)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def get(
        self, agent_id: str, session_id: str, key: str, *, tenant_id: str | None = None
    ) -> ScratchpadEntry | None:
        query = select(MemoryScratchRow).where(
            MemoryScratchRow.agent_id == agent_id,
            MemoryScratchRow.session_id == session_id,
            MemoryScratchRow.key == key,
        )
        if tenant_id is not None:
            query = query.where(MemoryScratchRow.tenant_id == tenant_id)
        async with self._sessionmaker() as session:
            row = (await session.execute(query)).scalar_one_or_none()
        return self._entry(row) if row is not None else None

    async def put(
        self,
        agent_id: str,
        session_id: str,
        key: str,
        value: str,
        *,
        tenant_id: str | None = None,
    ) -> ScratchpadEntry:
        """Upsert — the natural key IS the table's primary key, so the
        conflict target is exact and the newest write wins."""
        effective = tenant_id or DEFAULT_TENANT
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    pg_insert(MemoryScratchRow)
                    .values(
                        agent_id=agent_id,
                        session_id=session_id,
                        key=key,
                        tenant_id=effective,
                        value=value,
                        updated_at=datetime.now(UTC),
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            MemoryScratchRow.agent_id,
                            MemoryScratchRow.session_id,
                            MemoryScratchRow.key,
                        ],
                        set_={"value": value, "updated_at": datetime.now(UTC)},
                    )
                    .returning(
                        MemoryScratchRow.agent_id,
                        MemoryScratchRow.session_id,
                        MemoryScratchRow.key,
                        MemoryScratchRow.value,
                        MemoryScratchRow.updated_at,
                    )
                )
            ).one()
            await session.commit()
        return ScratchpadEntry(
            agent_id=row.agent_id,
            session_id=row.session_id,
            key=row.key,
            value=row.value,
            updated_at=row.updated_at,
        )

    async def delete(
        self, agent_id: str, session_id: str, key: str, *, tenant_id: str | None = None
    ) -> bool:
        query = select(MemoryScratchRow).where(
            MemoryScratchRow.agent_id == agent_id,
            MemoryScratchRow.session_id == session_id,
            MemoryScratchRow.key == key,
        )
        if tenant_id is not None:
            query = query.where(MemoryScratchRow.tenant_id == tenant_id)
        async with self._sessionmaker() as session:
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    @staticmethod
    def _entry(row: MemoryScratchRow) -> ScratchpadEntry:
        return ScratchpadEntry(
            agent_id=row.agent_id,
            session_id=row.session_id,
            key=row.key,
            value=row.value,
            updated_at=row.updated_at,
        )


class SqlAuthRepo:
    """Users, sessions, API keys, and stored credentials (S2, ADR 0009 §3).

    Secret discipline at this boundary: passwords and API keys arrive as
    *hashes* (the caller hashed them via `security/`), sessions are stored
    by token hash, and credentials carry only the AES-GCM envelope — this
    class never sees plaintext and could not leak it if it tried.

    Every credential/api-key mutation takes a `tenant_id` and scopes the
    WHERE clause with it: a foreign id resolves to `None`/`False`, which
    callers map to 404 (no existence leak).
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # --- tenants -----------------------------------------------------------

    async def create_tenant(self, tenant_id: str, name: str) -> None:
        async with self._sessionmaker() as session:
            session.add(TenantRow(id=tenant_id, name=name))
            await session.commit()

    async def get_tenant(self, tenant_id: str) -> TenantRow | None:
        async with self._sessionmaker() as session:
            return await session.get(TenantRow, tenant_id)

    # --- users ---------------------------------------------------------------

    async def create_user(
        self,
        *,
        tenant_id: str,
        email: str,
        display_name: str = "",
        password_hash: str | None = None,
        role: TenantRole = "member",
    ) -> UserAccount:
        user = UserAccount(
            id=_uuid(),
            tenant_id=tenant_id,
            email=email,
            display_name=display_name,
            role=role,
            created_at=_now(),
            password_hash=password_hash,
        )
        async with self._sessionmaker() as session:
            session.add(self._user_row(user))
            await session.commit()
        return user

    async def get_user(self, user_id: str) -> UserAccount | None:
        async with self._sessionmaker() as session:
            row = await session.get(UserRow, user_id)
        return self._load_user(row) if row is not None else None

    async def get_user_by_email(self, email: str) -> UserAccount | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(select(UserRow).where(UserRow.email == email))
            ).scalar_one_or_none()
        return self._load_user(row) if row is not None else None

    async def list_users(self, tenant_id: str) -> list[UserAccount]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(UserRow)
                    .where(UserRow.tenant_id == tenant_id)
                    .order_by(UserRow.created_at)
                )
            ).scalars()
            return [self._load_user(row) for row in rows]

    async def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        role: TenantRole | None = None,
        password_hash: str | None = None,
    ) -> UserAccount | None:
        """Patch only the provided fields; None if the user doesn't exist."""
        async with self._sessionmaker() as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                return None
            if display_name is not None:
                row.display_name = display_name
            if role is not None:
                row.role = role
            if password_hash is not None:
                row.password_hash = password_hash
            await session.commit()
            return self._load_user(row)

    async def delete_user(self, user_id: str) -> bool:
        """False if the user has API keys or credentials (caller maps to
        409 — deleting the creator would orphan their keys); sessions
        cascade at the DB level."""
        async with self._sessionmaker() as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                return False
            has_keys = (
                await session.execute(
                    select(ApiKeyRow.id).where(ApiKeyRow.user_id == user_id).limit(1)
                )
            ).scalar_one_or_none()
            has_creds = (
                await session.execute(
                    select(CredentialRow.id).where(CredentialRow.created_by == user_id).limit(1)
                )
            ).scalar_one_or_none()
            if has_keys is not None or has_creds is not None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    # --- sessions ----------------------------------------------------------

    async def create_session(
        self, *, user_id: str, token_hash: str, expires_at: datetime
    ) -> SessionRecord:
        record = SessionRecord(id=_uuid(), user_id=user_id, expires_at=expires_at)
        async with self._sessionmaker() as session:
            session.add(
                SessionRow(
                    id=record.id,
                    user_id=user_id,
                    token_hash=token_hash,
                    expires_at=expires_at,
                )
            )
            await session.commit()
        return record

    async def get_session_by_token_hash(
        self, token_hash: str
    ) -> tuple[SessionRecord, UserAccount] | None:
        """Unexpired session joined with its user — the session-cookie
        lookup. Expired rows simply don't match (no refresh semantics)."""
        async with self._sessionmaker() as session:
            result = (
                await session.execute(
                    select(SessionRow, UserRow)
                    .join(UserRow, UserRow.id == SessionRow.user_id)
                    .where(
                        SessionRow.token_hash == token_hash,
                        SessionRow.expires_at > _now(),
                    )
                )
            ).first()
        if result is None:
            return None
        session_row, user_row = result
        record = SessionRecord(
            id=session_row.id,
            user_id=session_row.user_id,
            expires_at=session_row.expires_at,
            created_at=session_row.created_at,
        )
        return record, self._load_user(user_row)

    async def delete_session(self, session_id: str) -> bool:
        async with self._sessionmaker() as session:
            row = await session.get(SessionRow, session_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    # --- api keys ------------------------------------------------------------

    async def create_api_key(
        self, *, tenant_id: str, user_id: str, name: str, key_hash: str, key_prefix: str
    ) -> ApiKeyRecord:
        record = ApiKeyRecord(
            id=_uuid(),
            tenant_id=tenant_id,
            user_id=user_id,
            name=name,
            key_prefix=key_prefix,
            created_at=_now(),
        )
        async with self._sessionmaker() as session:
            session.add(
                ApiKeyRow(
                    id=record.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    name=name,
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    created_at=record.created_at,
                )
            )
            await session.commit()
        return record

    async def get_api_key_by_hash(self, key_hash: str) -> tuple[ApiKeyRecord, UserAccount] | None:
        """Unrevoked key joined with its owning user — the Bearer lookup."""
        async with self._sessionmaker() as session:
            result = (
                await session.execute(
                    select(ApiKeyRow, UserRow)
                    .join(UserRow, UserRow.id == ApiKeyRow.user_id)
                    .where(ApiKeyRow.key_hash == key_hash, ApiKeyRow.revoked_at.is_(None))
                )
            ).first()
            if result is None:
                return None
            key_row, user_row = result
            await session.execute(
                update(ApiKeyRow).where(ApiKeyRow.id == key_row.id).values(last_used_at=_now())
            )
            await session.commit()
        return self._load_api_key(key_row), self._load_user(user_row)

    async def list_api_keys(self, tenant_id: str) -> list[ApiKeyRecord]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(ApiKeyRow)
                    .where(ApiKeyRow.tenant_id == tenant_id)
                    .order_by(ApiKeyRow.created_at.desc())
                )
            ).scalars()
            return [self._load_api_key(row) for row in rows]

    async def revoke_api_key(self, key_id: str, tenant_id: str) -> bool:
        """Tenant-scoped revoke; False = no such key in this tenant (404)."""
        async with self._sessionmaker() as session:
            result = await session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == key_id, ApiKeyRow.tenant_id == tenant_id)
                .values(revoked_at=_now())
                .returning(ApiKeyRow.id)
            )
            revoked = result.scalar_one_or_none() is not None
            await session.commit()
        return revoked

    # --- stored credentials ----------------------------------------------------

    async def create_credential(
        self,
        *,
        tenant_id: str,
        name: str,
        provider: str,
        ciphertext: dict[str, Any],
        created_by: str,
    ) -> StoredCredentialRecord:
        record = StoredCredentialRecord(
            id=_uuid(),
            tenant_id=tenant_id,
            name=name,
            provider=provider,
            ciphertext=ciphertext,
            created_by=created_by,
            created_at=_now(),
            updated_at=_now(),
        )
        async with self._sessionmaker() as session:
            session.add(
                CredentialRow(
                    id=record.id,
                    tenant_id=tenant_id,
                    name=name,
                    provider=provider,
                    ciphertext=ciphertext,
                    created_by=created_by,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return record

    async def get_credential(
        self, credential_id: str, tenant_id: str
    ) -> StoredCredentialRecord | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(CredentialRow).where(
                        CredentialRow.id == credential_id,
                        CredentialRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return self._load_credential(row) if row is not None else None

    async def list_credentials(self, tenant_id: str) -> list[StoredCredentialRecord]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(CredentialRow)
                    .where(CredentialRow.tenant_id == tenant_id)
                    .order_by(CredentialRow.created_at)
                )
            ).scalars()
            return [self._load_credential(row) for row in rows]

    async def update_credential(
        self,
        credential_id: str,
        tenant_id: str,
        *,
        name: str | None = None,
        ciphertext: dict[str, Any] | None = None,
    ) -> StoredCredentialRecord | None:
        """Patch name and/or re-encrypt the secret; None = not in this tenant."""
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(CredentialRow).where(
                        CredentialRow.id == credential_id,
                        CredentialRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if name is not None:
                row.name = name
            if ciphertext is not None:
                row.ciphertext = ciphertext
            row.updated_at = _now()
            await session.commit()
            return self._load_credential(row)

    async def revoke_credential(self, credential_id: str, tenant_id: str) -> bool:
        """Tenant-scoped revoke; False = no such credential here (404)."""
        async with self._sessionmaker() as session:
            result = await session.execute(
                update(CredentialRow)
                .where(CredentialRow.id == credential_id, CredentialRow.tenant_id == tenant_id)
                .values(revoked_at=_now())
                .returning(CredentialRow.id)
            )
            revoked = result.scalar_one_or_none() is not None
            await session.commit()
        return revoked

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _user_row(user: UserAccount) -> UserRow:
        return UserRow(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            display_name=user.display_name,
            password_hash=user.password_hash,
            role=user.role,
            created_at=user.created_at or _now(),
        )

    @staticmethod
    def _load_user(row: UserRow) -> UserAccount:
        return UserAccount(
            id=row.id,
            tenant_id=row.tenant_id,
            email=row.email,
            display_name=row.display_name,
            role=cast(TenantRole, row.role),
            created_at=row.created_at,
            password_hash=row.password_hash,
        )

    @staticmethod
    def _load_api_key(row: ApiKeyRow) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            name=row.name,
            key_prefix=row.key_prefix,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
        )

    @staticmethod
    def _load_credential(row: CredentialRow) -> StoredCredentialRecord:
        return StoredCredentialRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            provider=row.provider,
            ciphertext=dict(row.ciphertext),
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            revoked_at=row.revoked_at,
        )


class SqlRunQueue:
    """Postgres run queue (ADR 0008 §2-3): `SKIP LOCKED` claims, worker
    leases, idempotent cancel requests.

    The sweep *decides nothing* — it returns expired run_ids and the worker
    policy requeues (0 events) or terminal-fails (any events) via
    `requeue`/`ack`, so two workers racing on the same expired lease both
    see the message claimed until a decision is recorded."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def enqueue(self, message: RunQueueMessage) -> None:
        async with self._sessionmaker() as session:
            session.add(RunQueueRow(run_id=message.run_id, payload=message.model_dump(mode="json")))
            await session.commit()

    async def enqueue_resume(self, run_id: str, resume: ResumeRequest) -> None:
        """JSONB `||` MERGE of `resume` into the acked (paused) run's
        payload, then flip the row back to pending (S10, ADR 0010 §4). The
        payload is the only place the enqueue-time principal/deadline
        survive, so this merges — never replaces. Unknown run_id: no-op
        (the API route has already 404'd; a raced row is absorbed)."""
        async with self._sessionmaker() as session:
            await session.execute(
                update(RunQueueRow)
                .where(RunQueueRow.run_id == run_id)
                .values(
                    # asyncpg cannot infer a dict bind's type inside
                    # jsonb_build_object — give the merge object an explicit
                    # JSONB-typed bindparam instead.
                    payload=RunQueueRow.payload.op("||")(
                        bindparam(
                            "resume_merge",
                            {"resume": resume.model_dump(mode="json")},
                            type_=JSONB,
                        )
                    ),
                    status="pending",
                    claimed_by=None,
                    claimed_at=None,
                    lease_until=None,
                )
            )
            await session.commit()

    async def claim(self, worker_id: str, lease: timedelta) -> RunQueueMessage | None:
        """One statement: pick the oldest pending message, lock it
        (SKIP LOCKED — a competing claimant moves on), mark claimed with a
        lease, return its payload."""
        now = _now()
        next_pending = (
            select(RunQueueRow.id)
            .where(RunQueueRow.status == "pending")
            .order_by(RunQueueRow.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        claim = (
            update(RunQueueRow)
            .where(RunQueueRow.id == next_pending)
            .values(
                status="claimed",
                claimed_by=worker_id,
                claimed_at=now,
                lease_until=now + lease,
            )
            .returning(RunQueueRow.payload)
        )
        async with self._sessionmaker() as session:
            payload = (await session.execute(claim)).scalar_one_or_none()
            await session.commit()
        return RunQueueMessage.model_validate(payload) if payload is not None else None

    async def ack(self, run_id: str) -> None:
        """Mark the claimed message done — only if still `claimed`. A run
        that pauses returns to the worker before the client resumes: a late
        ack here must never clobber a just-enqueued resume back to `done`
        (the resume would be lost and the blocking resume route would wait
        forever). `enqueue_resume` flips the row to `pending`; this guard
        makes the ack a no-op in that race."""
        async with self._sessionmaker() as session:
            await session.execute(
                update(RunQueueRow)
                .where(RunQueueRow.run_id == run_id, RunQueueRow.status == "claimed")
                .values(status="done")
            )
            await session.commit()

    async def renew(self, run_id: str, worker_id: str, lease: timedelta) -> bool:
        """Extend the lease iff still claimed by *this* worker — False means
        the lease was lost and the worker must stop the run."""
        async with self._sessionmaker() as session:
            result = await session.execute(
                update(RunQueueRow)
                .where(
                    RunQueueRow.run_id == run_id,
                    RunQueueRow.status == "claimed",
                    RunQueueRow.claimed_by == worker_id,
                )
                .values(lease_until=_now() + lease)
                .returning(RunQueueRow.id)
            )
            renewed = result.scalar_one_or_none() is not None
            await session.commit()
        return renewed

    async def requeue(self, run_id: str) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                update(RunQueueRow)
                .where(RunQueueRow.run_id == run_id, RunQueueRow.status == "claimed")
                .values(status="pending", claimed_by=None, claimed_at=None, lease_until=None)
            )
            await session.commit()

    async def pending_cancel(self, run_id: str) -> str | None:
        """Atomic pop: delete the cancel request and return its reason."""
        async with self._sessionmaker() as session:
            reason = (
                await session.execute(
                    delete(RunCancelRow)
                    .where(RunCancelRow.run_id == run_id)
                    .returning(RunCancelRow.reason)
                )
            ).scalar_one_or_none()
            await session.commit()
        return reason

    async def request_cancel(self, run_id: str, reason: str) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                pg_insert(RunCancelRow)
                .values(run_id=run_id, reason=reason)
                .on_conflict_do_nothing(index_elements=[RunCancelRow.run_id])
            )
            await session.commit()

    async def sweep(self, expired_before: datetime) -> list[str]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(RunQueueRow.run_id).where(
                        RunQueueRow.status == "claimed",
                        RunQueueRow.lease_until < expired_before,
                    )
                )
            ).scalars()
            return list(rows)


__all__ = [
    "SqlAgentRepo",
    "SqlWorkflowRepo",
    "SqlAuthRepo",
    "SqlConversationRepo",
    "SqlExecutionRepo",
    "SqlRunQueue",
    "create_sessionmaker",
]
