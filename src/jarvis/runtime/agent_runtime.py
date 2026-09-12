"""AgentRuntime — the orchestrator (ADR 0004).

Owns the loop: iteration cap, deadline, token budget, cancellation, memory,
persistence, tool execution and terminal event emission (`sink.finalize()`
exactly once). Strategies do exactly one model invocation per step and
accumulate usage into `ctx.usage`.

Blocking `/run` and streaming `/stream` both call `AgentRuntime.run()`; the
SSE route subscribes to the run's sink while the run executes as an asyncio
task, so both modes produce identical event sequences."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import jsonschema

from jarvis.domain.agent import AgentDefinition, AgentVersion, ToolBinding
from jarvis.domain.events import (
    EventSequenceError,
    IterationCompleted,
    IterationStarted,
    RunAwaitingInput,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCallCompleted,
    ToolCallDeclined,
    ToolCallFailed,
    ToolCallRequested,
    ToolCallStarted,
)
from jarvis.domain.execution import ExecutionCancelled, ExecutionContext, RunResult
from jarvis.domain.message import Message, ToolCall
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.events.bus import InProcessEventBus, InProcessEventSink
from jarvis.models.errors import ModelAbortedError, ModelError
from jarvis.ports.model import ModelClient, ModelProviderFactory
from jarvis.ports.queue import ResumeRequest
from jarvis.ports.repository import ConversationRepo, ExecutionRepo
from jarvis.ports.strategy import (
    AskHumanStep,
    FinishStep,
    StepOutcome,
    StrategyRegistry,
    ToolCallsStep,
)
from jarvis.ports.tools import ToolRegistry
from jarvis.prompt.engine import PromptContext, PromptEngine
from jarvis.runtime.limits import RunLimits
from jarvis.runtime.llm_trace import LlmTraceBuffer, TracedModelClient
from jarvis.tools.mcp.errors import McpResolutionError
from jarvis.tools.mcp.provider import McpTooling, McpToolProvider
from jarvis.tools.runtime import ToolRuntime

ErrorKind = Literal["max_iterations", "timeout", "model", "tool", "output_schema", "strategy"]

DEFAULT_AWAITING_INPUT_TIMEOUT_SECONDS = 86_400.0


@dataclass
class PauseOutcome:
    """Why the loop paused (S10, ADR 0010 §1). `awaiting_until` carries the
    deadline stamped on both the pause event and the run row."""

    reason: Literal["tool_approval", "strategy"]
    question: str = ""
    pending_calls: list[ToolCall] = field(default_factory=list)  # approval-gated calls
    awaiting_until: datetime | None = None
    pause_cursor: int | None = None  # the run.awaiting_input event's cursor


@dataclass
class LoopOutcome:
    kind: str  # "completed" | "failed" | "paused"
    final_message: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    iterations: int = 0
    pause: PauseOutcome | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        strategies: StrategyRegistry,
        tools: ToolRegistry,
        tool_runtime: ToolRuntime,
        models: ModelProviderFactory,
        prompt_engine: PromptEngine | None = None,
        bus: InProcessEventBus | None = None,
        executions: ExecutionRepo | None = None,
        conversations: ConversationRepo | None = None,
        limits: RunLimits | None = None,
        mcp: McpToolProvider | None = None,
        trace_llm: bool = False,
        trace_buffer: LlmTraceBuffer | None = None,
    ) -> None:
        self._strategies = strategies
        self._tools = tools
        self._tool_runtime = tool_runtime
        self._models = models
        self._prompt_engine = prompt_engine or PromptEngine()
        self._bus = bus or InProcessEventBus()
        self._executions = executions
        self._conversations = conversations
        self._limits = limits
        self._mcp = mcp
        self._trace_llm = trace_llm
        self.trace_buffer = trace_buffer
        self._live_tokens: dict[str, ExecutionContext] = {}

    @property
    def llm_trace_buffer(self) -> LlmTraceBuffer:
        """The container-shared trace registry (ADR 0014) — the trace route
        reads it after its own scoped-run authorization."""
        if self.trace_buffer is None:
            self.trace_buffer = LlmTraceBuffer()
        return self.trace_buffer

    @property
    def bus(self) -> InProcessEventBus:
        return self._bus

    def cancel(self, run_id: str, reason: str = "cancelled by user") -> bool:
        """Trigger a live run's token. Idempotent; False if run unknown."""
        ctx = self._live_tokens.get(run_id)
        if ctx is None:
            return False
        ctx.cancel.trigger(reason)
        return True

    def is_live(self, run_id: str) -> bool:
        return run_id in self._live_tokens

    # --- public API ---------------------------------------------------------

    async def run(
        self,
        version: AgentVersion,
        input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink | None = None,
    ) -> RunResult:
        """Execute one agent run to a terminal state. Never raises."""
        agent = version.snapshot
        ctx.temperature = agent.temperature
        started_at = datetime.now(UTC)
        self._live_tokens[ctx.run_id] = ctx
        sink = sink or await self._bus.get_or_create(ctx.run_id)

        if self._executions is not None:
            # A RUNNING row exists from the first event — /executions/{id}/cancel
            # and list filters must see live runs, not only finished ones.
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
                )
            )

        try:
            # Inside the try: credential resolution is IO (S2) and can fail —
            # the failure must become a persisted terminal `model` state
            # (D5), never an exception past the runtime (which the worker
            # would treat as a claim failure and retry forever).
            client = await self._models.resolve(agent.model, principal=ctx.principal)
            if self._trace_llm:
                client = TracedModelClient(client, ctx, buffer=self.llm_trace_buffer)
            # S4 (D38): MCP toolset resolution is the same seam, third
            # application — its failure is a persisted terminal `tool`
            # state naming the server, before any token is spent.
            tooling = await self._resolve_tooling(agent, ctx)
            result = await self._execute(
                version, input, ctx, sink, client, agent, started_at, tooling
            )
        except ExecutionCancelled as exc:
            result = await self._terminal_cancelled(ctx, sink, exc, started_at, input)
        except ModelAbortedError as exc:
            # An abort is a cancellation, not a failure (D5): the token fired
            # in the gap between the loop's checkpoint and the invocation —
            # the provider observed it first. Reclassify by the token's own
            # reason so the terminal is run.cancelled, never run.failed.
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
            # S4 (D38): the boundary fails honestly — exactly one persisted
            # terminal run.failed with error_kind="tool", the server named.
            result = await self._terminal_failed(ctx, sink, str(exc), "tool", started_at, input)
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
            # A paused run already wrote its awaiting_input row (mark, not
            # finish) — finish_run would stamp finished_at and clear the
            # pause deadline (S10).
            await self._executions.finish_run(result)
        return result

    # --- happy path ---------------------------------------------------------

    async def _execute(
        self,
        version: AgentVersion,
        input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        client: ModelClient,
        agent: AgentDefinition,
        started_at: datetime,
        tooling: McpTooling,
    ) -> RunResult:
        try:
            history, conversation_id = await self._load_memory(ctx, agent)

            messages = self._prompt_engine.build(
                PromptContext(
                    agent=agent,
                    input=input,
                    variables=ctx.variables,
                    history=history,
                    tools=self._bound_descriptors(agent, tooling.registry),
                    schema_in_prompt=(
                        agent.output_schema is not None
                        and client.capabilities.structured_output != "json_schema"
                    ),
                )
            )
            ctx.output_schema = agent.output_schema
            ctx.structured_mode = client.capabilities.structured_output

            await sink.append(
                RunStarted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    agent_id=agent.id,
                    agent_version_id=version.id,
                    session_id=ctx.session_id,
                    input=input,
                )
            )
            user_message = messages[-1]
            await self._save_message(ctx, user_message)
            if conversation_id:
                await self._conversations.append_message(conversation_id, user_message, ctx.run_id)  # type: ignore[union-attr]

            outcome = await self._loop(
                ctx, agent, client, sink, messages, conversation_id, tooling=tooling
            )
            return await self._after_loop(ctx, sink, outcome, started_at, input)
        finally:
            # S4 (D38): the segment's MCP connections close with the
            # segment — completed, failed, cancelled, or paused alike.
            await tooling.aclose()

    async def _after_loop(
        self,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        outcome: LoopOutcome,
        started_at: datetime,
        run_input: str,
    ) -> RunResult:
        """Shared tail for a fresh run and a resumed segment (S10)."""
        if outcome.kind == "paused":
            pause = outcome.pause
            if pause is None:  # the loop always sets it — defensive terminal
                return await self._terminal_failed(
                    ctx,
                    sink,
                    "internal error: paused without a pause outcome",
                    "model",
                    started_at,
                    run_input,
                    iterations=outcome.iterations,
                )
            # S10 (ADR 0010 §3): the loop returned WITHOUT finalize — the run
            # row flips to awaiting_input (the worker acks the queue row).
            # No terminal event: exactly-one-terminal still holds for the
            # whole run; the resumed segment continues the sequence.
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
                finished_at=None,  # paused, not finished
                event_cursor=pause.pause_cursor,
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

    async def _loop(
        self,
        ctx: ExecutionContext,
        agent: AgentDefinition,
        client: ModelClient,
        sink: InProcessEventSink,
        messages: list[Message],
        conversation_id: str | None,
        iteration: int = 0,
        *,
        tooling: McpTooling | None = None,  # the segment's resolved toolset (S4)
        entry: str = "fresh",  # "fresh" | "answer" | "batch" — resumed-segment entries (S10)
        pending_calls: list[ToolCall] | None = None,  # the resumed batch (approval)
        refusals: set[str] | None = None,  # call ids the human declined
    ) -> LoopOutcome:
        # S3 (D36): StrategyConfig.params flow verbatim through ctx.metadata —
        # the frozen step() Protocol takes no config argument, so the
        # orchestrator prepares the ctx (same pattern as output_schema).
        ctx.metadata["strategy_params"] = dict(agent.strategy.params)
        try:
            strategy = self._strategies.resolve(agent.strategy)
        except KeyError as exc:
            # D36: a snapshot whose strategy is gone (plugin uninstalled or
            # de-allow-listed) terminal-fails with its own kind — never an
            # exception past the runtime, never a worker retry loop.
            return LoopOutcome(kind="failed", error=str(exc), error_kind="strategy")
        segment_tooling = tooling or self._default_tooling()
        descriptors = self._bound_descriptors(agent, segment_tooling.registry)
        bindings = {binding.name: binding for binding in agent.enabled_tools()}
        repair_attempted = False

        while True:
            ctx.iteration = iteration
            ctx.check_limits()  # deadline / cancellation -> ExecutionCancelled
            if self._limits is not None:
                # platform-level budget (ADR 0004): the orchestrator owns limits;
                # the per-agent max_iterations cap is enforced at the loop bottom
                reason = self._limits.exceeded(ctx)
                if reason is not None:
                    return LoopOutcome(
                        kind="failed",
                        error=f"run limit exceeded: {reason}",
                        error_kind="max_iterations",
                        iterations=iteration,
                    )

            batch_refusals: set[str] | None = None
            if entry == "batch":
                # S10 (ADR 0010 §4): the approved (or refused) batch executes
                # inside the paused iteration — its IterationStarted is already
                # in the log, and tool.call.requested already fired before the
                # pause; neither is replayed.
                calls = list(pending_calls or [])
                batch_refusals = refusals
                entry = "fresh"
            else:
                if entry != "answer":
                    await sink.append(
                        IterationStarted(
                            event_id=_uuid(),
                            run_id=ctx.run_id,
                            created_at=_now(),
                            iteration=iteration,
                        )
                    )
                # entry == "answer": the strategy is re-invoked inside the
                # paused iteration (its IterationStarted is already in the
                # log) — the human's answer is already in `messages`.
                entry = "fresh"

                try:
                    step: StepOutcome = await strategy.step(
                        ctx, list(messages), client, descriptors, sink
                    )
                except (ExecutionCancelled, ModelAbortedError, ModelError):
                    # the outer handlers own those (their kinds, their retries)
                    raise
                except Exception as exc:  # noqa: BLE001 — D36: a broken plugin
                    # is a persisted terminal strategy failure, never a crash.
                    return LoopOutcome(
                        kind="failed",
                        error=f"strategy {agent.strategy.type!r} raised "
                        f"{type(exc).__name__}: {exc}",
                        error_kind="strategy",
                    )
                messages.append(step.assistant_message)
                for extra in step.messages:
                    messages.append(extra)
                await self._save_message(ctx, step.assistant_message)
                for extra in step.messages:
                    await self._save_message(ctx, extra)
                if conversation_id and self._conversations is not None:
                    for message in [step.assistant_message, *step.messages]:
                        await self._conversations.append_message(
                            conversation_id, message, ctx.run_id
                        )

                if isinstance(step, FinishStep):
                    final_text = step.assistant_message.text
                    if agent.output_schema is not None:
                        ok, problem = _validate_structured(final_text, agent.output_schema)
                        if not ok:
                            if repair_attempted:
                                return LoopOutcome(
                                    kind="failed",
                                    error=f"output did not match schema after repair: {problem}",
                                    error_kind="output_schema",
                                    iterations=iteration + 1,
                                )
                            repair_attempted = True
                            await self._iteration_done(sink, ctx, iteration)
                            messages.append(
                                Message(
                                    role="developer",
                                    content=(
                                        f"Your reply did not match the required JSON Schema: "
                                        f"{problem}. Reply again with a corrected single "
                                        "JSON object, no prose."
                                    ),
                                )
                            )
                            await self._save_message(ctx, messages[-1])
                            iteration += 1
                            continue
                    await self._iteration_done(sink, ctx, iteration)
                    return LoopOutcome(
                        kind="completed", final_message=final_text, iterations=iteration + 1
                    )

                if isinstance(step, AskHumanStep):
                    # S10 (ADR 0010 §3.2): the strategy wants a human answer. The
                    # assistant message is already persisted; pause the run.
                    return await self._pause(
                        sink,
                        ctx,
                        PauseOutcome(reason="strategy", question=step.question),
                        iteration,
                    )

                assert isinstance(step, ToolCallsStep)
                # S10 (ADR 0010 §3.1): approval is checked BEFORE the batch
                # executes. Every call gets its tool.call.requested frame; a
                # gated batch pauses with the gated subset pending — no
                # tool.call.started until the human answers.
                for call in step.tool_calls:
                    await sink.append(
                        ToolCallRequested(
                            event_id=_uuid(),
                            run_id=ctx.run_id,
                            created_at=_now(),
                            tool_call_id=call.id,
                            name=call.name,
                            arguments=dict(call.arguments),
                        )
                    )
                gated = [
                    call
                    for call in step.tool_calls
                    if self._approval_required(bindings, descriptors, call.name)
                ]
                if gated:
                    return await self._pause(
                        sink,
                        ctx,
                        PauseOutcome(reason="tool_approval", pending_calls=gated),
                        iteration,
                    )
                calls = step.tool_calls
                batch_refusals = None

            for call in calls:
                if batch_refusals and call.id in batch_refusals:
                    # S10/ADR 0011 §3: the human declined this call — the
                    # declined event records the *decision* (no
                    # started/completed; it never ran), the refusal tool
                    # message closes it in the model's context. Ungated
                    # calls in the batch still execute.
                    await sink.append(
                        ToolCallDeclined(
                            event_id=_uuid(),
                            run_id=ctx.run_id,
                            created_at=_now(),
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    tool_message = Message(
                        role="tool",
                        content=(
                            "The user declined this tool call, so it did not run. "
                            "Do not call this tool again for this request — "
                            "continue without it and answer from what you know."
                        ),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                    messages.append(tool_message)
                    await self._save_message(ctx, tool_message)
                    if conversation_id and self._conversations is not None:
                        await self._conversations.append_message(
                            conversation_id, tool_message, ctx.run_id
                        )
                    continue
                await sink.append(
                    ToolCallStarted(
                        event_id=_uuid(),
                        run_id=ctx.run_id,
                        created_at=_now(),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                binding = bindings.get(call.name)
                result = await segment_tooling.tool_runtime.execute(
                    call,
                    ToolContext(
                        run_id=ctx.run_id,
                        agent_id=agent.id,
                        session_id=ctx.session_id,
                        user_id=ctx.user_id,
                        variables=ctx.variables,
                        config=dict(binding.config) if binding else {},
                        cancel=ctx.cancel,
                    ),
                )
                tool_message = Message(
                    role="tool",
                    content=result.output,
                    tool_call_id=call.id,
                    name=call.name,
                )
                messages.append(tool_message)
                await self._save_message(ctx, tool_message)
                if self._executions is not None:
                    await self._executions.save_tool_execution(
                        ctx.run_id, result, dict(call.arguments)
                    )
                if conversation_id and self._conversations is not None:
                    await self._conversations.append_message(
                        conversation_id, tool_message, ctx.run_id
                    )
                await sink.append(
                    ToolCallCompleted(
                        event_id=_uuid(),
                        run_id=ctx.run_id,
                        created_at=_now(),
                        tool_call_id=call.id,
                        name=call.name,
                        output=result.output[:2000],
                        is_error=result.is_error,
                        latency_ms=result.latency_ms,
                    )
                    if not result.is_error
                    else ToolCallFailed(
                        event_id=_uuid(),
                        run_id=ctx.run_id,
                        created_at=_now(),
                        tool_call_id=call.id,
                        name=call.name,
                        error=result.output,
                        kind=result.metadata.get("kind", "internal"),
                    )
                )

            await self._iteration_done(sink, ctx, iteration)
            iteration += 1
            if iteration >= agent.max_iterations:
                return LoopOutcome(
                    kind="failed",
                    error=f"agent did not finish within max_iterations={agent.max_iterations}",
                    error_kind="max_iterations",
                    iterations=iteration,
                )

    # --- pause (S10, ADR 0010) ----------------------------------------------

    async def _pause(
        self,
        sink: InProcessEventSink,
        ctx: ExecutionContext,
        outcome: PauseOutcome,
        iteration: int,
    ) -> LoopOutcome:
        """Emit run.awaiting_input and end the segment. No IterationCompleted —
        the iteration is unfinished; the resumed segment re-counts iterations
        from the persisted iteration.started events."""
        awaiting_until = datetime.now(UTC) + timedelta(
            seconds=(
                self._limits.awaiting_input_timeout_seconds
                if self._limits is not None
                else DEFAULT_AWAITING_INPUT_TIMEOUT_SECONDS
            )
        )
        outcome.awaiting_until = awaiting_until
        outcome.pause_cursor = await sink.append(
            RunAwaitingInput(
                event_id=_uuid(),
                run_id=ctx.run_id,
                created_at=_now(),
                reason=outcome.reason,
                question=outcome.question,
                pending_calls=outcome.pending_calls,
                awaiting_until=awaiting_until,
            )
        )
        return LoopOutcome(kind="paused", iterations=iteration, pause=outcome)

    def _approval_required(
        self,
        bindings: dict[str, ToolBinding],
        descriptors: list[ToolDescriptor],
        tool_name: str,
    ) -> bool:
        """S10 (ADR 0010 §3.1): the binding's config wins over the
        descriptor's annotations — any bound tool can be gated per-agent
        without a new tool."""
        binding = bindings.get(tool_name)
        if binding is not None and "requires_approval" in binding.config:
            return bool(binding.config["requires_approval"])
        descriptor = next((d for d in descriptors if d.name == tool_name), None)
        if descriptor is not None:
            return bool(descriptor.annotations.get("requires_approval", False))
        return False

    # --- resume (S10, ADR 0010 §4) -------------------------------------------

    async def resume(
        self,
        version: AgentVersion,
        run_id: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        resume: ResumeRequest,
    ) -> RunResult:
        """Continue a paused run with the human's answer. Never raises.

        The segment continues the run's gapless event sequence — the caller
        supplies a sink seeded at `next_event_sequence` (a fresh sink would
        restart at 0). Counters re-seed from the run row and the event log so
        limits span the whole chain (usage accumulates across segments, the
        original deadline rides ctx). Model resolution is inside the try
        (D28): a resumed segment fails terminally, never escapes to the
        worker as a claim failure. The loop may pause AGAIN — resume returns
        `awaiting_input` and a later resume continues the chain."""
        agent = version.snapshot
        ctx.temperature = agent.temperature
        started_at = datetime.now(UTC)
        run_input = ""
        self._live_tokens[run_id] = ctx
        try:
            if self._executions is not None:
                row = await self._executions.get(run_id)
                if row is not None:
                    # started_at/input ride the chain, not the segment; usage
                    # accumulates across segments (platform limits re-seed too).
                    started_at = row.started_at or started_at
                    run_input = row.input
                    ctx.usage = row.total_usage.model_copy()
            client = await self._models.resolve(agent.model, principal=ctx.principal)
            if self._trace_llm:
                client = TracedModelClient(client, ctx, buffer=self.llm_trace_buffer)
            tooling = await self._resolve_tooling(agent, ctx)  # S4 (D38): re-resolve per segment
            result = await self._resume_segment(
                run_input, ctx, sink, client, agent, started_at, resume, tooling
            )
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
            # S4 (D38): same honest boundary as a fresh run — one persisted
            # terminal `tool` failure, the server named.
            result = await self._terminal_failed(ctx, sink, str(exc), "tool", started_at, run_input)
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
            self._live_tokens.pop(run_id, None)

        if self._executions is not None and result.status != "awaiting_input":
            # mirror run(): a paused result already wrote its awaiting_input row
            await self._executions.finish_run(result)
        return result

    async def _resume_segment(
        self,
        run_input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        client: ModelClient,
        agent: AgentDefinition,
        started_at: datetime,
        resume: ResumeRequest,
        tooling: McpTooling,
    ) -> RunResult:
        try:
            return await self._resume_segment_body(
                run_input, ctx, sink, client, agent, started_at, resume, tooling
            )
        finally:
            # S4 (D38): the resumed segment's MCP connections close with the
            # segment; a later resume re-resolves and reconnects.
            await tooling.aclose()

    async def _resume_segment_body(
        self,
        run_input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        client: ModelClient,
        agent: AgentDefinition,
        started_at: datetime,
        resume: ResumeRequest,
        tooling: McpTooling,
    ) -> RunResult:
        # Re-seed the chain's counters from the durable log: the open
        # iteration is the last one that STARTED (no IterationCompleted was
        # emitted at the pause) and the pause event carries the gated batch.
        pause_event: RunAwaitingInput | None = None
        iteration_starts = 0
        if self._executions is not None:
            async for event in self._executions.list_events(ctx.run_id):
                if isinstance(event, IterationStarted):
                    iteration_starts += 1
                elif isinstance(event, RunAwaitingInput):
                    pause_event = event
        open_iteration = max(iteration_starts - 1, 0)
        ctx.iteration = open_iteration

        conversation_id = await self._resume_conversation(ctx, agent)
        messages = await self._rebuild_messages(
            agent, ctx, client, ctx.run_id, conversation_id, tooling.registry
        )

        entry = "answer"
        pending_calls: list[ToolCall] | None = None
        refusals: set[str] | None = None
        if (
            resume.kind in ("tool_approval", "decisions")
            and pause_event is not None
            and pause_event.reason == "tool_approval"
            and pause_event.pending_calls
        ):
            # The full batch rides the persisted assistant message (the pause
            # event only carries the gated subset). Approve → the whole batch
            # executes (ADR 0010 §3.1); reject → gated calls are refused, the
            # ungated remainder still executes. `decisions` (ADR 0011) splits
            # the batch per call — a call absent from the map is declined
            # (default-deny: silence never approves).
            batch = _pause_batch(messages) or list(pause_event.pending_calls)
            entry, pending_calls = "batch", batch
            if resume.kind == "decisions":
                decided = resume.decisions or {}
                refusals = {
                    call.id for call in pause_event.pending_calls if not decided.get(call.id, False)
                }
            elif not resume.approved:
                refusals = {call.id for call in pause_event.pending_calls}
        else:
            # A strategy pause answers with content. A mismatched resume kind
            # (approval request against a strategy pause) degrades to the
            # same path — the loop re-invokes the strategy with whatever the
            # human sent.
            if resume.kind == "content" and resume.content is not None:
                answer = Message(role="user", content=resume.content)
                messages.append(answer)
                await self._save_message(ctx, answer)
                if conversation_id and self._conversations is not None:
                    await self._conversations.append_message(conversation_id, answer, ctx.run_id)

        outcome = await self._loop(
            ctx,
            agent,
            client,
            sink,
            messages,
            conversation_id,
            iteration=open_iteration,
            tooling=tooling,
            entry=entry,
            pending_calls=pending_calls,
            refusals=refusals,
        )
        return await self._after_loop(ctx, sink, outcome, started_at, run_input)

    async def _resume_conversation(
        self, ctx: ExecutionContext, agent: AgentDefinition
    ) -> str | None:
        """Look up the run's conversation WITHOUT creating one (the pause
        already created it on the original segment when memory is on)."""
        if not agent.memory.enabled or not ctx.session_id or self._conversations is None:
            return None
        return await self._conversations.find(agent.id, ctx.session_id)

    async def _rebuild_messages(
        self,
        agent: AgentDefinition,
        ctx: ExecutionContext,
        client: ModelClient,
        run_id: str,
        conversation_id: str | None,
        registry: ToolRegistry | None = None,
    ) -> list[Message]:
        """Reconstruct the model's context for a resumed segment: the system
        prompt plus the run transcript. With memory on, the conversation
        history is the superset (prior sessions folded in with this run's
        messages — the loop appends to both); otherwise the run's own
        persisted transcript is exactly what the original context was. The
        system prompt renders against the SEGMENT's registry (S4: the
        fresh segment's resolved tools must match what it can execute)."""
        if conversation_id and self._conversations is not None:
            transcript = await self._conversations.history(conversation_id)
        elif self._executions is not None:
            transcript = await self._executions.list_messages(run_id)
        else:
            transcript = []
        system = self._prompt_engine.render_system(
            PromptContext(
                agent=agent,
                input="",
                variables=ctx.variables,
                tools=self._bound_descriptors(agent, registry),
                schema_in_prompt=(
                    agent.output_schema is not None
                    and client.capabilities.structured_output != "json_schema"
                ),
            )
        )
        messages = [Message(role="system", content=system)] if system else []
        messages.extend(transcript)
        return messages

    # --- memory -------------------------------------------------------------

    async def _load_memory(
        self, ctx: ExecutionContext, agent: AgentDefinition
    ) -> tuple[list[Message], str | None]:
        if not agent.memory.enabled or not ctx.session_id or self._conversations is None:
            return [], None
        conversation_id = await self._conversations.get_or_create(
            agent.id, ctx.session_id, tenant_id=ctx.tenant_id
        )
        history = await self._conversations.history(conversation_id)
        return history, conversation_id

    # --- persistence helpers --------------------------------------------------

    async def _save_message(self, ctx: ExecutionContext, message: Message) -> None:
        if self._executions is not None:
            await self._executions.save_message(ctx.run_id, message)

    async def _iteration_done(
        self, sink: InProcessEventSink, ctx: ExecutionContext, iteration: int
    ) -> None:
        await sink.append(
            IterationCompleted(
                event_id=_uuid(),
                run_id=ctx.run_id,
                created_at=_now(),
                iteration=iteration,
                usage=ctx.usage,
            )
        )

    # --- terminals -------------------------------------------------------------

    async def _terminal_cancelled(
        self,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        exc: ExecutionCancelled,
        started_at: datetime,
        run_input: str = "",
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
        run_input: str = "",
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
        """finalize() exactly once; a second attempt means a bug upstream —
        surface the last cursor instead of corrupting the run's result."""
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
        )

    def _bound_descriptors(
        self, agent: AgentDefinition, registry: ToolRegistry | None = None
    ) -> list[ToolDescriptor]:
        """Descriptors for the bound tool names, against the SEGMENT's
        registry (S4: the resolved view includes MCP wrappers); unknown
        binding names are skipped, not fatal."""
        registry = registry or self._default_tooling().registry
        descriptors = []
        for binding in agent.enabled_tools():
            try:
                descriptors.append(registry.get(binding.name).descriptor)
            except KeyError:
                continue  # unknown binding names are skipped, not fatal
        return descriptors

    async def _resolve_tooling(self, agent: AgentDefinition, ctx: ExecutionContext) -> McpTooling:
        """S4 (D38): eager per-segment MCP resolution. No MCP configured or
        no `mcp__*` bindings → the builtin registry, byte-identical to
        today; any resolution failure raises McpResolutionError (caught in
        run()/resume()'s try — D28 pattern)."""
        if self._mcp is None:
            return self._default_tooling()
        return await self._mcp.resolve(
            agent.enabled_tools(), tenant_id=ctx.tenant_id, principal=ctx.principal
        )

    def _default_tooling(self) -> McpTooling:
        return McpTooling(
            registry=self._tools, tool_runtime=self._tool_runtime, aclose=_no_op_close
        )


def _validate_structured(text: str, schema: dict[str, Any]) -> tuple[bool, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"reply is not valid JSON: {exc.msg}"
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        return False, exc.message
    return True, ""


def _pause_batch(messages: list[Message]) -> list[ToolCall]:
    """The tool-call batch the run paused on: the newest assistant message
    that carries tool_calls (persisted before the pause). Empty when the
    transcript holds no such message."""
    for message in reversed(messages):
        if message.role == "assistant" and message.tool_calls:
            return list(message.tool_calls)
    return []


async def _no_op_close() -> None:
    """The default tooling's aclose — builtins hold no connections."""


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["AgentRuntime"]
