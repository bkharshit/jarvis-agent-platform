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
from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import InstrumentedAttribute

from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import ExecutionStatus, RunResult
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolResult
from jarvis.persistence.models import (
    AgentExecutionRow,
    AgentRow,
    AgentVersionRow,
    ConversationRow,
    ExecutionEventRow,
    MessageRow,
    RunCancelRow,
    RunQueueRow,
    ToolExecutionRow,
)
from jarvis.ports.queue import RunQueueMessage

_EVENT_ADAPTER: TypeAdapter[ExecutionEvent] = TypeAdapter(ExecutionEvent)


def create_sessionmaker(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(database_url)
    return async_sessionmaker(engine, expire_on_commit=False)


# --- serialization helpers (the only place domain meets JSONB) --------------


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


class SqlAgentRepo:
    """Agents with append-only version history (snapshot-vs-reference)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def create(self, definition: AgentDefinition) -> AgentDefinition:
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
                )
            )
            session.add(self._version_row(version))
            await session.commit()
        return definition

    async def get(self, agent_id: str) -> AgentDefinition | None:
        async with self._sessionmaker() as session:
            row = await session.get(AgentRow, agent_id)
            if row is None:
                return None
            snapshot = await self._snapshot_for(session, row)
        return AgentDefinition.model_validate(snapshot)

    async def get_by_name(self, name: str) -> AgentDefinition | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(select(AgentRow).where(AgentRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                return None
            snapshot = await self._snapshot_for(session, row)
        return AgentDefinition.model_validate(snapshot)

    async def list_agents(self, limit: int = 50, offset: int = 0) -> list[AgentDefinition]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(AgentRow).order_by(AgentRow.created_at).limit(limit).offset(offset)
                )
            ).scalars()
            agents = []
            for row in rows:
                snapshot = await self._snapshot_for(session, row)
                agents.append(AgentDefinition.model_validate(snapshot))
        return agents

    async def update_and_publish(
        self, definition: AgentDefinition, label: str = ""
    ) -> AgentVersion:
        """Update mutable fields and append a new immutable snapshot."""
        async with self._sessionmaker() as session:
            row = await session.get(AgentRow, definition.id)
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

    async def get_version(self, agent_id: str, version: int) -> AgentVersion | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(AgentVersionRow).where(
                        AgentVersionRow.agent_id == agent_id,
                        AgentVersionRow.version == version,
                    )
                )
            ).scalar_one_or_none()
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

    async def latest_version(self, agent_id: str) -> AgentVersion | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(AgentVersionRow)
                    .where(AgentVersionRow.agent_id == agent_id)
                    .order_by(AgentVersionRow.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._load_version(row)

    async def list_versions(self, agent_id: str) -> list[AgentVersion]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(AgentVersionRow)
                    .where(AgentVersionRow.agent_id == agent_id)
                    .order_by(AgentVersionRow.version)
                )
            ).scalars()
            return [self._load_version(row) for row in rows]

    async def delete(self, agent_id: str) -> bool:
        """False if the agent has executions (caller maps to 409)."""
        if await self.has_executions(agent_id):
            return False
        async with self._sessionmaker() as session:
            row = await session.get(AgentRow, agent_id)
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
        requeued run are a no-op — the row is already running)."""
        async with self._sessionmaker() as session:
            await session.execute(
                update(AgentExecutionRow)
                .where(AgentExecutionRow.id == run_id, AgentExecutionRow.status == "queued")
                .values(status="running", started_at=started_at)
            )
            await session.commit()

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
            await session.commit()

    async def get(self, run_id: str) -> RunResult | None:
        async with self._sessionmaker() as session:
            row = await session.get(AgentExecutionRow, run_id)
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
    ) -> list[RunResult]:
        query = select(AgentExecutionRow)
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
        )

    @staticmethod
    def _load_run(row: AgentExecutionRow) -> RunResult:
        from jarvis.domain.message import Usage

        return RunResult(
            run_id=row.id,
            agent_id=row.agent_id,
            status=cast(ExecutionStatus, row.status),
            input=row.input,
            agent_version_id=row.agent_version_id,
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

    async def get_or_create(self, agent_id: str, session_id: str) -> str:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ConversationRow).where(
                        ConversationRow.agent_id == agent_id,
                        ConversationRow.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return row.id
            row = ConversationRow(id=_uuid(), agent_id=agent_id, session_id=session_id)
            session.add(row)
            await session.commit()
            return row.id

    async def find(self, agent_id: str, session_id: str) -> str | None:
        """Look up without creating — the read-side pair of get_or_create."""
        async with self._sessionmaker() as session:
            return (
                await session.execute(
                    select(ConversationRow.id).where(
                        ConversationRow.agent_id == agent_id,
                        ConversationRow.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()

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

    async def history(self, conversation_id: str, limit: int | None = None) -> list[Message]:
        query = select(MessageRow).where(MessageRow.conversation_id == conversation_id)
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
        async with self._sessionmaker() as session:
            await session.execute(
                update(RunQueueRow).where(RunQueueRow.run_id == run_id).values(status="done")
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
    "SqlConversationRepo",
    "SqlExecutionRepo",
    "SqlRunQueue",
    "create_sessionmaker",
]
