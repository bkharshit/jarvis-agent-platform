"""WorkflowRuntime — the sibling executor (ADR 0015 §5).

Walks a workflow version's graph one node at a time: agent nodes ride
`AgentRuntime._run_segment` through the narrow `SegmentRunner` protocol
(one segment — no terminal, no row writes; the loop is reused, never
copied), tool nodes execute through the unchanged `ToolRuntime`, condition
nodes evaluate closed operators against the upstream output. The runtime
owns the run's lifecycle exactly like `AgentRuntime.run`: RUNNING row at
start, one terminal event, `finish_run` at the end — and it never raises.

Limits transpose ADR 0004 one level up: the walk owns the node cap
(`max_node_executions`, D44), cancellation and deadline checks run per
node via `ctx.check_limits()`, and usage accumulates in the shared `ctx`
so the token budget spans the whole workflow run. Resolution (pinned
agent versions D42, tool-node MCP bindings) happens eagerly inside the
try — the D28 pattern, fifth application: a resolution failure is a
persisted terminal state, never an escape past the runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from jarvis.domain.agent import AgentVersion
from jarvis.domain.events import (
    EventSequenceError,
    IterationStarted,
    NodeCompleted,
    NodeStarted,
    RunAwaitingInput,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
    ToolCallStarted,
)
from jarvis.domain.execution import ExecutionCancelled, ExecutionContext, RunResult
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext
from jarvis.domain.workflow import (
    AgentNodeConfig,
    ConditionNodeConfig,
    ToolNodeConfig,
    WorkflowDefinition,
    WorkflowVersion,
    evaluate_condition,
    render_template,
)
from jarvis.events.bus import InProcessEventBus, InProcessEventSink, NodeSink
from jarvis.models.errors import ModelAbortedError, ModelError
from jarvis.ports.queue import ResumeRequest
from jarvis.ports.repository import ExecutionRepo
from jarvis.runtime.agent_runtime import ErrorKind, LoopOutcome
from jarvis.runtime.limits import RunLimits
from jarvis.tools.mcp.errors import McpResolutionError
from jarvis.tools.mcp.provider import McpTooling, McpToolProvider
from jarvis.tools.runtime import ToolRuntime


class SegmentRunner(Protocol):
    """The narrow structural view of `AgentRuntime` the workflow runtime
    consumes for agent nodes (ADR 0015 §5): one segment — client/tooling
    resolution, prompt build, the loop, message persistence — returning the
    outcome WITHOUT a terminal event or row writes; plus the live cancel
    surface. Fakes stand in for unit tests."""

    async def _run_segment(
        self,
        version: AgentVersion,
        input: str,
        ctx: ExecutionContext,
        sink: Any,  # InProcessEventSink | NodeSink — stamped per node
        started: RunStarted | None = None,
    ) -> LoopOutcome: ...

    async def resume_segment(
        self,
        version: AgentVersion,
        run_id: str,
        ctx: ExecutionContext,
        sink: Any,
        resume: ResumeRequest,
        *,
        open_iteration: int | None = None,
    ) -> LoopOutcome: ...

    def cancel(self, run_id: str, reason: str = "cancelled by user") -> bool: ...

    def is_live(self, run_id: str) -> bool: ...


class AgentVersionLoader(Protocol):
    """Structural view of the agent repo: resolve a pinned version snapshot
    (D42) — the same seam the worker's `VersionLoader` already names."""

    async def get_version_by_id(self, version_id: str) -> AgentVersion | None: ...


@dataclass
class _ResumeState:
    """Where a paused walk stands, replayed from the event log: the paused
    node, upstream outputs, the node count (the cap spans the chain), and the
    paused node's own open iteration for the inner segment's re-seed."""

    node_id: str
    outputs: dict[str, Any]
    last_output: str
    count: int
    open_iteration: int
    resume: ResumeRequest


class WorkflowRuntime:
    def __init__(
        self,
        *,
        runner: SegmentRunner,
        versions: AgentVersionLoader,
        tools: Any,  # ToolRegistry — the default tool-node view's registry
        tool_runtime: ToolRuntime,
        bus: InProcessEventBus | None = None,
        executions: ExecutionRepo | None = None,
        limits: RunLimits | None = None,
        mcp: McpToolProvider | None = None,
    ) -> None:
        self._runner = runner
        self._versions = versions
        self._tools = tools
        self._tool_runtime = tool_runtime
        self._bus = bus or InProcessEventBus()
        self._executions = executions
        self._limits = limits
        self._mcp = mcp
        self._live_tokens: dict[str, ExecutionContext] = {}

    @property
    def bus(self) -> InProcessEventBus:
        return self._bus

    def cancel(self, run_id: str, reason: str = "cancelled by user") -> bool:
        """Trigger a live workflow run's token. Idempotent; False if unknown."""
        ctx = self._live_tokens.get(run_id)
        if ctx is None:
            return False
        ctx.cancel.trigger(reason)
        return True

    def is_live(self, run_id: str) -> bool:
        return run_id in self._live_tokens

    # --- public API -------------------------------------------------------------

    async def run(
        self,
        version: WorkflowVersion,
        input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink | None = None,
    ) -> RunResult:
        """Execute one workflow run to a terminal state. Never raises."""
        wf = version.snapshot
        started_at = datetime.now(UTC)
        self._live_tokens[ctx.run_id] = ctx
        sink = sink or await self._bus.get_or_create(ctx.run_id)

        if self._executions is not None:
            await self._executions.create_run(
                RunResult(
                    run_id=ctx.run_id,
                    agent_id=ctx.agent_id,
                    status="running",
                    input=input,
                    agent_version_id=ctx.agent_version_id,
                    tenant_id=ctx.tenant_id,
                    session_id=ctx.session_id,
                    trace_id=ctx.trace_id,
                    started_at=started_at,
                    metadata=dict(ctx.metadata),  # {"kind": "workflow"} (D41)
                )
            )

        try:
            versions, tooling = await self._resolve(wf, ctx)
            await sink.append(
                RunStarted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    agent_id=ctx.agent_id,
                    agent_version_id=version.id,
                    session_id=ctx.session_id,
                    input=input,
                )
            )
            try:
                outcome = await self._walk(wf, versions, tooling, input, ctx, sink)
                result = await self._terminal(ctx, sink, outcome, started_at, input)
            finally:
                # D38: the tool-node toolset's MCP connections close with the
                # run — completed, failed, cancelled, or paused alike; a later
                # resume re-resolves.
                await tooling.aclose()
        except ExecutionCancelled as exc:
            result = await self._terminal_cancelled(ctx, sink, exc, started_at, input)
        except ModelAbortedError as exc:
            result = await self._terminal_cancelled(
                ctx,
                sink,
                ExecutionCancelled(ctx.cancel.reason or str(exc)),
                started_at,
                input,
            )
        except ModelError as exc:
            result = await self._terminal_failed(ctx, sink, str(exc), "model", started_at, input)
        except McpResolutionError as exc:
            # D38, fourth application: eager tool-node resolution fails the
            # run before any node runs — one persisted terminal, server named.
            result = await self._terminal_failed(ctx, sink, str(exc), "tool", started_at, input)
        except EventSequenceError as exc:
            # A terminal from inside a node (a NodeSink refusal) is a bug —
            # surface it honestly rather than double-finalize.
            result = await self._terminal_failed(
                ctx, sink, f"internal error: {exc}", "model", started_at, input
            )
        except Exception as exc:  # noqa: BLE001 — the run never crashes callers
            result = await self._terminal_failed(
                ctx,
                sink,
                f"internal error: {type(exc).__name__}: {exc}",
                "model",
                started_at,
                input,
            )
        finally:
            self._live_tokens.pop(ctx.run_id, None)

        if self._executions is not None and result.status != "awaiting_input":
            # a paused run already wrote its awaiting_input row (S10 mirror)
            await self._executions.finish_run(result)
        return result

    async def resume(
        self,
        version: WorkflowVersion,
        run_id: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        resume: ResumeRequest,
    ) -> RunResult:
        """Continue a workflow run paused INSIDE an agent node (S10
        composition, ADR 0015 §7): re-resolve the graph, replay the event log
        for the walk's state (upstream outputs, the paused node, its own
        iteration), resume the paused node's segment, then continue the walk
        from there. Never raises — the same handlers as `run()`."""
        wf = version.snapshot
        started_at = datetime.now(UTC)
        run_input = ""
        self._live_tokens[ctx.run_id] = ctx
        if self._executions is not None:
            row = await self._executions.get(run_id)
            if row is not None:
                # the original workflow input (downstream templates reference
                # it) and the chain's usage-so-far ride the row
                started_at = row.started_at or started_at
                run_input = row.input
        try:
            versions, tooling = await self._resolve(wf, ctx)
            try:
                state = await self._resume_state(ctx, resume)
                if state is None:
                    raise ModelError("internal error: no pause frame in the event log")
                outcome = await self._walk(
                    wf, versions, tooling, run_input, ctx, sink, resume_state=state
                )
                result = await self._terminal(ctx, sink, outcome, started_at, run_input)
            finally:
                await tooling.aclose()
        except ExecutionCancelled as exc:
            result = await self._terminal_cancelled(ctx, sink, exc, started_at, run_input)
        except ModelAbortedError as exc:
            result = await self._terminal_cancelled(
                ctx,
                sink,
                ExecutionCancelled(ctx.cancel.reason or str(exc)),
                started_at,
                run_input,
            )
        except ModelError as exc:
            result = await self._terminal_failed(
                ctx, sink, str(exc), "model", started_at, run_input
            )
        except McpResolutionError as exc:
            result = await self._terminal_failed(ctx, sink, str(exc), "tool", started_at, run_input)
        except EventSequenceError as exc:
            result = await self._terminal_failed(
                ctx, sink, f"internal error: {exc}", "model", started_at, run_input
            )
        except Exception as exc:  # noqa: BLE001 — the run never crashes callers
            result = await self._terminal_failed(
                ctx,
                sink,
                f"internal error: {type(exc).__name__}: {exc}",
                "model",
                started_at,
                run_input,
            )
        finally:
            self._live_tokens.pop(ctx.run_id, None)

        if self._executions is not None and result.status != "awaiting_input":
            await self._executions.finish_run(result)
        return result

    async def _resume_state(
        self, ctx: ExecutionContext, resume: ResumeRequest
    ) -> _ResumeState | None:
        """Rebuild the walk's position from the durable event log: the pause
        frame names the paused node (its NodeSink stamped node_id); node
        outputs, the walk's node count, and the paused node's OWN iteration
        count all replay from the same sequence."""
        if self._executions is None:
            return None
        outputs: dict[str, Any] = {}
        last_output = ""
        started_count = 0
        pause: RunAwaitingInput | None = None
        async for event in self._executions.list_events(ctx.run_id):
            if isinstance(event, NodeStarted):
                started_count += 1
            elif isinstance(event, NodeCompleted):
                outputs[event.node_id] = event.output
                last_output = event.output
            elif isinstance(event, RunAwaitingInput):
                pause = event
        if pause is None or pause.node_id is None:
            return None
        node_iterations = 0
        async for event in self._executions.list_events(ctx.run_id):
            if isinstance(event, IterationStarted) and event.node_id == pause.node_id:
                node_iterations += 1
        return _ResumeState(
            node_id=pause.node_id,
            outputs=outputs,
            last_output=last_output,
            count=started_count,
            open_iteration=max(node_iterations - 1, 0),
            resume=resume,
        )

    # --- resolution (D28 pattern, fifth application) ----------------------------

    async def _resolve(
        self, wf: WorkflowDefinition, ctx: ExecutionContext
    ) -> tuple[dict[str, AgentVersion], McpTooling]:
        """Load every agent node's pinned version and resolve the tool-node
        toolset BEFORE the walk — any failure raises; `run()`'s handlers
        turn it into the persisted terminal."""
        versions: dict[str, AgentVersion] = {}
        for node in wf.nodes:
            if node.type != "agent":
                continue
            config = _agent_config(node)
            if config.agent_version_id is None:
                raise ModelError(
                    f"node {node.id!r} has no pinned agent version — publish the workflow"
                )
            version = await self._versions.get_version_by_id(config.agent_version_id)
            if version is None:
                raise ModelError(
                    f"agent version {config.agent_version_id!r} pinned by node "
                    f"{node.id!r} not found"
                )
            if version.agent_id != config.agent_id:
                raise ModelError(
                    f"node {node.id!r} pins agent {config.agent_id!r} but version "
                    f"{config.agent_version_id!r} belongs to agent {version.agent_id!r}"
                )
            versions[node.id] = version
        # Tool-node toolset: eager MCP resolution for `mcp__*` bindings (D38);
        # the provider's no-MCP path returns the builtin view byte-identically.
        if self._mcp is not None:
            tooling = await self._mcp.resolve(
                [_tool_config(node).binding for node in wf.nodes if node.type == "tool"],
                tenant_id=ctx.tenant_id,
                principal=ctx.principal,
            )
        else:
            tooling = McpTooling(
                registry=self._tools, tool_runtime=self._tool_runtime, aclose=_no_op_close
            )
        return versions, tooling

    # --- the walk (D44: sequential, deterministic) --------------------------------

    async def _walk(
        self,
        wf: WorkflowDefinition,
        versions: dict[str, AgentVersion],
        tooling: McpTooling,
        input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        resume_state: _ResumeState | None = None,
    ) -> LoopOutcome:
        nodes = {node.id: node for node in wf.nodes}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for edge in wf.edges:
            outgoing[edge.from_node].append(edge.to_node)
        current: str | None = wf.start_node_id
        count = 0
        last_output = ""
        outputs: dict[str, Any] = {}

        if resume_state is not None:
            # The S10 composition (ADR 0015 §7): resume the PAUSED node's
            # segment without re-emitting its node.started — it started in the
            # original segment; the gapless sequence continues from the
            # resumed loop's first event. Upstream outputs ride the replay.
            state = resume_state
            outputs, last_output, count = state.outputs, state.last_output, state.count
            node = nodes[state.node_id]
            node_sink = NodeSink(sink, node.id)
            outcome = await self._runner.resume_segment(
                versions[node.id],
                ctx.run_id,
                ctx,
                node_sink,
                state.resume,
                open_iteration=state.open_iteration,
            )
            if outcome.kind == "paused":
                return LoopOutcome(kind="paused", iterations=count, pause=outcome.pause)
            if outcome.kind == "failed":
                # D43: no node.failed — the inner failure becomes the run's
                # terminal, the node named in the error string.
                return LoopOutcome(
                    kind="failed",
                    error=f"node {node.id!r}: {outcome.error or 'unknown error'}",
                    error_kind=outcome.error_kind,
                    iterations=count,
                )
            output = outcome.final_message or ""
            outputs[node.id] = output
            last_output = output
            await node_sink.append(
                NodeCompleted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    node_id=node.id,
                    node_type="agent",
                    output=output[:2000],
                )
            )
            current = _next_node(outgoing[node.id])

        while current is not None:
            ctx.check_limits()  # deadline / cancellation are the run's own
            if self._limits is not None and self._limits.max_total_tokens is not None:
                # The token budget spans the WHOLE workflow run — usage
                # accumulates in the shared ctx across nodes (ADR 0015 §5).
                # (The platform iteration cap is the agent loop's own; the
                # workflow's cap is max_node_executions below.)
                total = ctx.usage.input_tokens + ctx.usage.output_tokens
                if total > self._limits.max_total_tokens:
                    return LoopOutcome(
                        kind="failed",
                        error=f"run limit exceeded: token_budget ({self._limits.max_total_tokens})",
                        error_kind="max_iterations",
                        iterations=count,
                    )
            count += 1
            if count > wf.max_node_executions:
                # ADR 0004 transposed (D44): the executor owns the cap.
                return LoopOutcome(
                    kind="failed",
                    error=f"workflow exceeded max_node_executions={wf.max_node_executions}",
                    error_kind="max_iterations",
                    iterations=count - 1,
                )
            node = nodes[current]
            node_sink = NodeSink(sink, node.id)
            await node_sink.append(
                NodeStarted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    node_id=node.id,
                    node_type=node.type,
                )
            )

            if node.type == "condition":
                # A condition node's routes ARE its edges — the graph's
                # outgoing-edge list is not consulted (canvas display only).
                cond_config = _condition_config(node)
                next_id = self._route_condition(cond_config, last_output)
                outputs[node.id] = next_id
                await node_sink.append(
                    NodeCompleted(
                        event_id=_uuid(),
                        run_id=ctx.run_id,
                        created_at=_now(),
                        node_id=node.id,
                        node_type=node.type,
                        output=next_id,
                    )
                )
                current = next_id
                continue

            if node.type == "tool":
                tool_config = _tool_config(node)
                result = await self._execute_tool_node(
                    tool_config, outputs, input, ctx, node_sink, tooling
                )
                outputs[node.id] = result.output
                last_output = result.output
                await node_sink.append(
                    NodeCompleted(
                        event_id=_uuid(),
                        run_id=ctx.run_id,
                        created_at=_now(),
                        node_id=node.id,
                        node_type="tool",
                        output=result.output[:2000],
                        is_error=result.is_error,
                    )
                )
                current = _next_node(outgoing[node.id])
                continue

            # --- agent node -----------------------------------------------------
            agent_config = _agent_config(node)
            values = {"input": input, "node": outputs, **ctx.variables}
            node_input = render_template(agent_config.input_template, values)
            outcome = await self._runner._run_segment(
                versions[node.id], node_input, ctx, node_sink, started=None
            )
            if outcome.kind == "paused":
                # Human-in-the-loop inside a node (S10 composition): the
                # pause frame already rode the NodeSink with the node's
                # node_id; the run row flips in `_terminal`.
                return LoopOutcome(kind="paused", iterations=count, pause=outcome.pause)
            if outcome.kind == "failed":
                # D43: no node.failed — the inner failure becomes the run's
                # terminal, the node named in the error string.
                return LoopOutcome(
                    kind="failed",
                    error=f"node {node.id!r}: {outcome.error or 'unknown error'}",
                    error_kind=outcome.error_kind,
                    iterations=count,
                )
            output = outcome.final_message or ""
            outputs[node.id] = output
            last_output = output
            await node_sink.append(
                NodeCompleted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    node_id=node.id,
                    node_type="agent",
                    output=output[:2000],
                )
            )
            current = _next_node(outgoing[node.id])

        # Dead end: the walk's last output is the workflow's final message.
        return LoopOutcome(kind="completed", final_message=last_output, iterations=count)

    def _route_condition(self, config: ConditionNodeConfig, text: str) -> str:
        """First matching route wins; else_node is required (domain)."""
        for route in config.routes:
            if evaluate_condition(route.when.operator, route.when.value, text):
                return route.to_node
        return config.else_node

    async def _execute_tool_node(
        self,
        config: ToolNodeConfig,
        outputs: dict[str, Any],
        input: str,
        ctx: ExecutionContext,
        node_sink: NodeSink,
        tooling: McpTooling,
    ) -> Any:
        binding = config.binding
        values = {"input": input, "node": outputs, **ctx.variables}
        arguments = {
            key: render_template(value, values) if isinstance(value, str) else value
            for key, value in config.arguments.items()
        }
        call = ToolCall(id=_uuid(), name=binding.name, arguments=arguments)
        await node_sink.append(
            ToolCallRequested(
                event_id=_uuid(),
                run_id=ctx.run_id,
                created_at=_now(),
                tool_call_id=call.id,
                name=call.name,
                arguments=dict(call.arguments),
            )
        )
        await node_sink.append(
            ToolCallStarted(
                event_id=_uuid(),
                run_id=ctx.run_id,
                created_at=_now(),
                tool_call_id=call.id,
                name=call.name,
            )
        )
        context = ToolContext(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            variables=ctx.variables,
            config=dict(binding.config),
            cancel=ctx.cancel,
        )
        result = await tooling.tool_runtime.execute(call, context)
        if self._executions is not None:
            await self._executions.save_tool_execution(ctx.run_id, result, dict(call.arguments))
        if result.is_error:
            await node_sink.append(
                ToolCallFailed(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    tool_call_id=call.id,
                    name=call.name,
                    error=result.output,
                    kind=result.metadata.get("kind", "internal"),
                )
            )
        else:
            await node_sink.append(
                ToolCallCompleted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    tool_call_id=call.id,
                    name=call.name,
                    output=result.output[:2000],
                    latency_ms=result.latency_ms,
                )
            )
        # An errored tool node does NOT fail the walk: node.completed carries
        # is_error=True and the walk continues — downstream nodes see the
        # error text as upstream output (composition semantics, ADR 0015 §3).
        return result

    # --- terminals (the AgentRuntime mirror) -------------------------------------

    async def _terminal(
        self,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        outcome: LoopOutcome,
        started_at: datetime,
        run_input: str,
    ) -> RunResult:
        if outcome.kind == "paused":
            pause = outcome.pause
            if pause is None:  # the walk always sets it — defensive
                return await self._terminal_failed(
                    ctx,
                    sink,
                    "internal error: paused without a pause outcome",
                    "model",
                    started_at,
                    run_input,
                    iterations=outcome.iterations,
                )
            if self._executions is not None and pause.awaiting_until is not None:
                await self._executions.mark_awaiting_input(
                    ctx.run_id, pause.awaiting_until, total_usage=ctx.usage
                )
            return RunResult(
                run_id=ctx.run_id,
                agent_id=ctx.agent_id,
                status="awaiting_input",
                input=run_input,
                agent_version_id=ctx.agent_version_id,
                tenant_id=ctx.tenant_id,
                session_id=ctx.session_id,
                trace_id=ctx.trace_id,
                total_usage=ctx.usage,
                iterations=outcome.iterations,
                started_at=started_at,
                finished_at=None,
                event_cursor=pause.pause_cursor,
                metadata=dict(ctx.metadata),
            )
        if outcome.kind == "completed":
            cursor = await self._finalize(
                sink,
                RunCompleted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    final_message=outcome.final_message or "",
                    total_usage=ctx.usage,
                    iterations=outcome.iterations,
                ),
            )
            return self._result(
                ctx,
                "succeeded",
                outcome.final_message,
                outcome.iterations,
                cursor,
                started_at,
                result_input=run_input,
            )
        return await self._terminal_failed(
            ctx,
            sink,
            outcome.error or "unknown error",
            outcome.error_kind or "model",
            started_at,
            run_input,
            iterations=outcome.iterations,
        )

    async def _terminal_cancelled(
        self,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        exc: ExecutionCancelled,
        started_at: datetime,
        run_input: str,
    ) -> RunResult:
        reason = exc.reason or "cancelled"
        if reason == "deadline exceeded":
            cursor = await self._finalize(
                sink,
                RunFailed(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    error="run deadline exceeded",
                    error_kind="timeout",
                    total_usage=ctx.usage,
                ),
            )
            return self._result(
                ctx,
                "timed_out",
                None,
                ctx.iteration,
                cursor,
                started_at,
                error="run deadline exceeded",
                error_kind="timeout",
            )
        cursor = await self._finalize(
            sink,
            RunCancelled(
                event_id=_uuid(),
                run_id=ctx.run_id,
                created_at=_now(),
                reason=reason,
                total_usage=ctx.usage,
            ),
        )
        return self._result(
            ctx, "cancelled", None, ctx.iteration, cursor, started_at, result_input=run_input
        )

    async def _terminal_failed(
        self,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        error: str,
        error_kind: ErrorKind,
        started_at: datetime,
        run_input: str,
        iterations: int = 0,
    ) -> RunResult:
        cursor = await self._finalize(
            sink,
            RunFailed(
                event_id=_uuid(),
                run_id=ctx.run_id,
                created_at=_now(),
                error=error,
                error_kind=error_kind,
                total_usage=ctx.usage,
            ),
        )
        return self._result(
            ctx,
            "failed",
            None,
            iterations,
            cursor,
            started_at,
            error=error,
            error_kind=error_kind,
        )

    async def _finalize(self, sink: InProcessEventSink, event: Any) -> int:
        try:
            return await sink.finalize(event)
        except EventSequenceError:
            return len(sink.events) - 1

    def _result(
        self,
        ctx: ExecutionContext,
        status: str,
        final_message: str | None,
        iterations: int,
        cursor: int,
        started_at: datetime,
        result_input: str = "",
        error: str | None = None,
        error_kind: str | None = None,
    ) -> RunResult:
        return RunResult(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            status=status,  # type: ignore[arg-type]
            input=result_input,
            agent_version_id=ctx.agent_version_id,
            tenant_id=ctx.tenant_id,
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            final_message=final_message,
            total_usage=ctx.usage,
            iterations=iterations,
            error=error,
            error_kind=error_kind,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            event_cursor=cursor,
            metadata=dict(ctx.metadata),
        )


def _agent_config(node: Any) -> AgentNodeConfig:
    assert isinstance(node.config, AgentNodeConfig)
    return node.config


def _tool_config(node: Any) -> ToolNodeConfig:
    assert isinstance(node.config, ToolNodeConfig)
    return node.config


def _condition_config(node: Any) -> ConditionNodeConfig:
    assert isinstance(node.config, ConditionNodeConfig)
    return node.config


def _next_node(outgoing: list[str]) -> str | None:
    """The single successor a sequential walk follows (first edge in
    definition order — fan-out is deferred, D44); None ends the walk."""
    return outgoing[0] if outgoing else None


async def _no_op_close() -> None:
    """The default tooling's aclose — builtins hold no connections."""


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["WorkflowRuntime"]
