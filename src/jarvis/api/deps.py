"""DI container — one `AppContainer` per app instance (plan §2, no globals).

Wiring order: settings → engine/sessionmaker → repos → event bus (persist =
`SqlExecutionRepo.append_event`, so the sink cursor is the durable
`execution_events.cursor`) → tool registry (3 builtins) → model provider
factory → strategy registry → `AgentRuntime` with `RunLimits` from Settings
(ADR 0004: the orchestrator owns limits).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from jarvis.config import Settings
from jarvis.events.bus import InProcessEventBus
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.persistence.repositories import (
    SqlAgentRepo,
    SqlConversationRepo,
    SqlExecutionRepo,
    SqlRunQueue,
)
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.runtime.limits import RunLimits
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
    bus: InProcessEventBus
    tools: InMemoryToolRegistry
    models: DefaultModelProviderFactory
    strategies: DefaultStrategyRegistry
    runtime: AgentRuntime
    limits: RunLimits

    @classmethod
    def from_settings(
        cls, settings: Settings, *, mock_provider: Any | None = None
    ) -> AppContainer:
        """`mock_provider` injects a shared MockModelProvider (tests / demo);
        production wiring passes nothing."""
        engine = create_async_engine(settings.database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        agents = SqlAgentRepo(sessionmaker)
        executions = SqlExecutionRepo(sessionmaker)
        conversations = SqlConversationRepo(sessionmaker)
        queue = SqlRunQueue(sessionmaker)

        registry = InMemoryToolRegistry()
        for tool in (CalculatorTool(), CurrentTimeTool(), HttpGetTool()):
            registry.register(tool)

        limits = RunLimits(
            max_iterations=settings.run_max_iterations,
            max_total_tokens=settings.run_max_total_tokens,
        )
        # The persist callback makes the sink cursor the durable
        # execution_events.cursor (SSE Last-Event-ID, ADR 0003). Sinks are
        # never dropped in Phase 1 — /stream resume relies on them.
        bus = InProcessEventBus(persist=executions.append_event)
        models = DefaultModelProviderFactory(mock_provider=mock_provider)
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
        return cls(
            settings=settings,
            engine=engine,
            agents=agents,
            executions=executions,
            conversations=conversations,
            queue=queue,
            bus=bus,
            tools=registry,
            models=models,
            strategies=strategies,
            runtime=runtime,
            limits=limits,
        )

    async def aclose(self) -> None:
        await self.engine.dispose()


def get_container(request: Request) -> AppContainer:
    """FastAPI dependency handing out the app's container."""
    container: AppContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("container not initialised — app must run through its lifespan")
    return container


__all__ = ["AppContainer", "get_container"]
