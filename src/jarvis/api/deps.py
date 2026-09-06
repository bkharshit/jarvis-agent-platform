"""DI container — one `AppContainer` per app instance (plan §2, no globals).

Wiring order: settings → engine/sessionmaker → repos → event bus (persist =
`SqlExecutionRepo.append_event`, so the sink cursor is the durable
`execution_events.cursor`) → tool registry (3 builtins) → model provider
factory → strategy registry → `AgentRuntime` with `RunLimits` from Settings
(ADR 0004: the orchestrator owns limits).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from jarvis.config import Settings
from jarvis.events.bus import InProcessEventBus
from jarvis.events.pg_notify import PgEventStream, PgNotifier
from jarvis.models.credentials import DefaultCredentialResolver
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.stored import DatabaseStoredResolver
from jarvis.persistence.repositories import (
    SqlAgentRepo,
    SqlAuthRepo,
    SqlConversationRepo,
    SqlExecutionRepo,
    SqlRunQueue,
)
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.runtime.limits import RunLimits
from jarvis.runtime.worker import Worker, worker_persist
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.builtin.current_time import CurrentTimeTool
from jarvis.tools.builtin.http_get import HttpGetTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime


@dataclass
class AppContainer:
    settings: Settings
    engine: AsyncEngine
    agents: SqlAgentRepo
    executions: SqlExecutionRepo
    conversations: SqlConversationRepo
    queue: SqlRunQueue
    auth: SqlAuthRepo
    notifier: PgNotifier
    streams: PgEventStream
    bus: InProcessEventBus
    tools: InMemoryToolRegistry
    models: DefaultModelProviderFactory
    strategies: DefaultStrategyRegistry
    runtime: AgentRuntime
    limits: RunLimits
    worker: Worker
    _worker_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings, *, mock_provider: Any | None = None) -> AppContainer:
        """`mock_provider` injects a shared MockModelProvider (tests / demo);
        production wiring passes nothing."""
        engine = create_async_engine(settings.database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        agents = SqlAgentRepo(sessionmaker)
        executions = SqlExecutionRepo(sessionmaker)
        conversations = SqlConversationRepo(sessionmaker)
        queue = SqlRunQueue(sessionmaker)
        auth = SqlAuthRepo(sessionmaker)
        notifier = PgNotifier(settings.database_url)
        # One LISTEN connection; the repo stays the sole SQL owner for
        # execution_events — the stream only tails through it.
        streams = PgEventStream(sessionmaker, settings.database_url)
        streams.replay_cursor_fn(executions.replay_with_cursor)
        streams.run_status_fn(executions.get)

        registry = InMemoryToolRegistry()
        for tool in (CalculatorTool(), CurrentTimeTool(), HttpGetTool()):
            registry.register(tool)

        limits = RunLimits(
            max_iterations=settings.run_max_iterations,
            max_total_tokens=settings.run_max_total_tokens,
            awaiting_input_timeout_seconds=settings.awaiting_input_timeout_seconds,
        )
        # The persist callback makes the sink cursor the durable
        # execution_events.cursor (SSE Last-Event-ID, ADR 0003). Sinks are
        # never dropped in Phase 1 — /stream resume relies on them.
        bus = InProcessEventBus(persist=executions.append_event)
        # The stored-credential backend (S2, ADR 0006) composes into the
        # same resolver seam; env refs keep resolving exactly as before.
        models = DefaultModelProviderFactory(
            mock_provider=mock_provider,
            credential_resolver=DefaultCredentialResolver(
                stored_resolver=DatabaseStoredResolver(sessionmaker, settings)
            ),
        )
        strategies = DefaultStrategyRegistry()
        runtime = AgentRuntime(
            strategies=strategies,
            tools=registry,
            tool_runtime=ToolRuntime(registry),
            models=models,
            bus=bus,
            executions=executions,
            conversations=conversations,
            limits=limits,
        )
        worker = Worker(
            queue=queue,
            versions=agents,
            executions=executions,
            runtime=runtime,
            # persist = durable append + pg_notify wake-up (ADR 0008 §4).
            persist=worker_persist(executions, notifier),
            concurrency=settings.worker_concurrency,
        )
        return cls(
            settings=settings,
            engine=engine,
            agents=agents,
            executions=executions,
            conversations=conversations,
            queue=queue,
            auth=auth,
            notifier=notifier,
            streams=streams,
            bus=bus,
            tools=registry,
            models=models,
            strategies=strategies,
            runtime=runtime,
            limits=limits,
            worker=worker,
        )

    # --- embedded worker lifecycle (S1, ADR 0008) ------------------------------

    async def start_worker(self) -> None:
        """Run the queue worker inside this process (embedded mode). The
        integration fixtures call this directly — ASGITransport skips the
        lifespan — and so does `create_app`'s lifespan."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self.worker.run_forever())

    async def stop_worker(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        await self.worker.aclose()

    async def aclose(self) -> None:
        await self.stop_worker()
        await self.streams.aclose()
        await self.notifier.aclose()
        await self.engine.dispose()


def get_container(request: Request) -> AppContainer:
    """FastAPI dependency handing out the app's container."""
    container: AppContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("container not initialised — app must run through its lifespan")
    return container


__all__ = ["AppContainer", "get_container"]
