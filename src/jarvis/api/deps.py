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
    SqlEvalRepo,
    SqlExecutionRepo,
    SqlKnowledgeRepo,
    SqlMcpServerRepo,
    SqlRunQueue,
    SqlScratchpadRepo,
    SqlTriggerRepo,
    SqlWorkflowRepo,
)
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.runtime.limits import RunLimits
from jarvis.runtime.llm_trace import LlmTraceBuffer
from jarvis.runtime.scheduler import TriggerScheduler
from jarvis.runtime.trigger_events import TriggerService
from jarvis.runtime.trigger_firing import TriggerFireService
from jarvis.runtime.worker import Worker, worker_persist
from jarvis.runtime.workflow_runtime import WorkflowRuntime
from jarvis.strategies.plugins import PluginLoadResult, load_strategy_plugins
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.builtin.current_time import CurrentTimeTool
from jarvis.tools.builtin.http_get import HttpGetTool
from jarvis.tools.builtin.memory import MemoryDeleteTool, MemoryGetTool, MemoryPutTool
from jarvis.tools.mcp.provider import McpToolProvider
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from jarvis.tools.subagents.provider import SubAgentToolProvider


@dataclass
class AppContainer:
    settings: Settings
    engine: AsyncEngine
    agents: SqlAgentRepo
    workflows: SqlWorkflowRepo
    executions: SqlExecutionRepo
    conversations: SqlConversationRepo
    scratchpad: SqlScratchpadRepo
    evaluations: SqlEvalRepo
    knowledge: SqlKnowledgeRepo
    triggers: SqlTriggerRepo
    queue: SqlRunQueue
    auth: SqlAuthRepo
    notifier: PgNotifier
    streams: PgEventStream
    bus: InProcessEventBus
    tools: InMemoryToolRegistry
    mcp_servers: SqlMcpServerRepo
    mcp: McpToolProvider
    sub_agents: SubAgentToolProvider
    models: DefaultModelProviderFactory
    strategies: DefaultStrategyRegistry
    strategy_plugins: PluginLoadResult
    runtime: AgentRuntime
    workflow_runtime: WorkflowRuntime
    limits: RunLimits
    worker: Worker
    firing: TriggerFireService
    trigger_events: TriggerService
    scheduler: TriggerScheduler
    _worker_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _scheduler_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings, *, mock_provider: Any | None = None) -> AppContainer:
        """`mock_provider` injects a shared MockModelProvider (tests / demo);
        production wiring passes nothing."""
        engine = create_async_engine(settings.database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        agents = SqlAgentRepo(sessionmaker)
        workflows = SqlWorkflowRepo(sessionmaker)
        executions = SqlExecutionRepo(sessionmaker)
        conversations = SqlConversationRepo(sessionmaker)
        scratchpad = SqlScratchpadRepo(sessionmaker)
        evaluations = SqlEvalRepo(sessionmaker)
        # S8 (ADR 0020 §1, D63): the knowledge repo rides the same
        # sessionmaker — strictly tenant-scoped, pgvector-backed.
        knowledge = SqlKnowledgeRepo(sessionmaker)
        triggers = SqlTriggerRepo(sessionmaker)
        queue = SqlRunQueue(sessionmaker)
        auth = SqlAuthRepo(sessionmaker)
        notifier = PgNotifier(settings.database_url)
        # One LISTEN connection; the repo stays the sole SQL owner for
        # execution_events — the stream only tails through it.
        streams = PgEventStream(sessionmaker, settings.database_url)
        streams.replay_cursor_fn(executions.replay_with_cursor)
        streams.run_status_fn(executions.get)

        registry = InMemoryToolRegistry()
        # S12 (D46): the memory_* builtins share the scratchpad repo —
        # reads/writes are ordinary tool calls, visible in tool_executions.
        for tool in (
            CalculatorTool(),
            CurrentTimeTool(),
            HttpGetTool(),
            MemoryGetTool(scratchpad),
            MemoryPutTool(scratchpad),
            MemoryDeleteTool(scratchpad),
        ):
            registry.register(tool)

        # The stored-credential backend (S2, ADR 0006) composes into the
        # same resolver seam; env refs keep resolving exactly as before.
        # Shared with the MCP provider (ADR 0013) — both are stateless.
        credential_resolver = DefaultCredentialResolver(
            stored_resolver=DatabaseStoredResolver(sessionmaker, settings)
        )
        models = DefaultModelProviderFactory(
            mock_provider=mock_provider,
            credential_resolver=credential_resolver,
        )
        # S4 (ADR 0012): MCP servers resolve per segment through the runtime's
        # provider seam; the same provider serves the API's probe route.
        # Stored header/env refs decrypt through the shared resolver.
        mcp_servers = SqlMcpServerRepo(sessionmaker)
        mcp = McpToolProvider(
            mcp_servers, registry, settings, credential_resolver=credential_resolver
        )
        # S14 (ADR 0018 §2): the sub-agent family resolves per segment beside
        # MCP — children are ordinary queue runs (D52), so the provider
        # shares the executions repo, the queue (request_cancel) and the
        # event stream (the blocking wait) with everything else.
        sub_agents = SubAgentToolProvider(
            agents,
            executions,
            queue,
            streams,
            transcripts=executions,  # the handoff transfer renders list_messages
            max_depth=settings.sub_agent_max_depth,
            max_transcript_chars=settings.handoff_max_transcript_chars,
        )

        limits = RunLimits(
            max_iterations=settings.run_max_iterations,
            max_total_tokens=settings.run_max_total_tokens,
            awaiting_input_timeout_seconds=settings.awaiting_input_timeout_seconds,
        )
        # The persist callback makes the sink cursor the durable
        # execution_events.cursor (SSE Last-Event-ID, ADR 0003). Sinks are
        # never dropped in Phase 1 — /stream resume relies on them.
        bus = InProcessEventBus(persist=executions.append_event)
        # S3 (D35): plugins load from installed entry points behind the
        # allow-list, exactly once here — every worker shares this container,
        # so a plugin strategy runs on any worker. Load failures are carried
        # in the result (capabilities reports them); `serve` boots with a
        # broken plugin on the allow-list.
        strategy_plugins = load_strategy_plugins(settings.strategy_plugin_allowlist)
        strategies = DefaultStrategyRegistry(
            extra=strategy_plugins.strategies,
            plugin_infos=strategy_plugins.loaded,
        )
        runtime = AgentRuntime(
            strategies=strategies,
            tools=registry,
            tool_runtime=ToolRuntime(registry),
            models=models,
            bus=bus,
            executions=executions,
            conversations=conversations,
            limits=limits,
            mcp=mcp,
            sub_agents=sub_agents,
            trace_llm=settings.llm_trace,
            trace_buffer=LlmTraceBuffer(),
        )
        # S6 (ADR 0015 §5): the sibling executor shares the agent runtime's
        # loop (its runner), bus, repos, limits and MCP provider.
        workflow_runtime = WorkflowRuntime(
            runner=runtime,
            versions=agents,
            tools=registry,
            tool_runtime=ToolRuntime(registry),
            bus=bus,
            executions=executions,
            limits=limits,
            mcp=mcp,
        )
        worker = Worker(
            queue=queue,
            versions=agents,
            executions=executions,
            runtime=runtime,
            # persist = durable append + pg_notify wake-up (ADR 0008 §4).
            persist=worker_persist(executions, notifier),
            workflow_runtime=workflow_runtime,
            workflow_versions=workflows,
            concurrency=settings.worker_concurrency,
        )
        # S13 (ADR 0019 §2): the firing service and the cron scheduler ride
        # the same repos — the scheduler only enqueues, so it is independent
        # of embedded_worker and started by the serve lifespan when enabled.
        firing = TriggerFireService(
            executions=executions,
            agents=agents,
            run_timeout_seconds=settings.run_timeout_seconds,
        )
        scheduler = TriggerScheduler(
            triggers=triggers,
            firing=firing,
            poll_seconds=settings.trigger_poll_seconds,
        )
        # S13 (D61): the event observer rides the executions repo's
        # post-commit finalize callback — the wiring loop (repo ← service ←
        # repo) resolves by late assignment, the observer is invoked only
        # after commit.
        trigger_events = TriggerService(
            triggers=triggers,
            firing=firing,
            max_depth=settings.trigger_max_depth,
            max_output_chars=settings.trigger_event_max_output_chars,
        )
        executions.on_finalized = trigger_events.on_run_finalized
        return cls(
            settings=settings,
            engine=engine,
            agents=agents,
            workflows=workflows,
            executions=executions,
            conversations=conversations,
            scratchpad=scratchpad,
            evaluations=evaluations,
            knowledge=knowledge,
            triggers=triggers,
            queue=queue,
            auth=auth,
            notifier=notifier,
            streams=streams,
            bus=bus,
            tools=registry,
            mcp_servers=mcp_servers,
            mcp=mcp,
            sub_agents=sub_agents,
            models=models,
            strategies=strategies,
            strategy_plugins=strategy_plugins,
            runtime=runtime,
            workflow_runtime=workflow_runtime,
            limits=limits,
            worker=worker,
            firing=firing,
            trigger_events=trigger_events,
            scheduler=scheduler,
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

    # --- trigger scheduler lifecycle (S13, ADR 0019 §3) -------------------------

    async def start_scheduler(self) -> None:
        """Run the cron scheduler inside this process — the serve lifespan's
        job (gated by `trigger_scheduler_enabled`), never the worker CLI's."""
        if self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(self.scheduler.run_forever())

    async def stop_scheduler(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

    async def aclose(self) -> None:
        await self.stop_worker()
        await self.stop_scheduler()
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
