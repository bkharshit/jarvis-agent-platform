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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import jsonschema
from pydantic import ValidationError

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
from jarvis.domain.knowledge import KnowledgeDataset
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.domain.tools import ToolContext, ToolDescriptor, ToolResult
from jarvis.events.bus import InProcessEventBus, InProcessEventSink
from jarvis.models.errors import ModelAbortedError, ModelError
from jarvis.ports.model import ModelClient, ModelProviderFactory
from jarvis.ports.queue import ResumeRequest
from jarvis.ports.repository import ConversationRepo, ExecutionRepo, KnowledgeRepo
from jarvis.ports.spans import SpanSink
from jarvis.ports.strategy import (
    AskHumanStep,
    FinishStep,
    StepOutcome,
    StrategyRegistry,
    ToolCallsStep,
)
from jarvis.ports.tools import ToolRegistry
from jarvis.prompt.engine import PromptContext, PromptEngine
from jarvis.runtime.knowledge.retrieval import KnowledgeRetrieval
from jarvis.runtime.limits import RunLimits
from jarvis.runtime.llm_trace import LlmTraceBuffer, TracedModelClient
from jarvis.runtime.memory import ConversationMemory, MemoryView
from jarvis.runtime.spans import SpanRecorder, TracingModelClient, status_for_run
from jarvis.tools.knowledge.provider import (
    RetrieverToolProvider,
    is_retriever_binding,
)
from jarvis.tools.mcp.errors import (
    RetrieverResolutionError,
    SubAgentResolutionError,
    ToolResolutionError,
)
from jarvis.tools.mcp.provider import McpTooling, McpToolProvider
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from jarvis.tools.subagents.provider import (
    FAMILY_PREFIXES,
    SubAgentTooling,
    SubAgentToolProvider,
)

ErrorKind = Literal["max_iterations", "timeout", "model", "tool", "output_schema", "strategy"]

DEFAULT_AWAITING_INPUT_TIMEOUT_SECONDS = 86_400.0

# S14 (D55): children never pause. The two refusal copies are parameterized
# by who declined: the human on the approval path (byte-identical copy), and
# the child's own non-interactivity (auto-decline, same shape).
_USER_DECLINE_COPY = (
    "The user declined this tool call, so it did not run. "
    "Do not call this tool again for this request — "
    "continue without it and answer from what you know."
)
_AUTO_DECLINE_COPY = (
    "No human approver is available in this run, so this call was not "
    "executed. Continue without it and answer from what you know."
)
_AUTO_ANSWER = "No human is available in this run — proceed from what you know."


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
class HandoffOutcome:
    """What a handoff tool reported through the metadata channel (D56) —
    the predecessor's terminal `run.completed` names the successor, and the
    linkage itself lives on the successor's row (parent_run_id +
    metadata.handoff)."""

    successor_run_id: str
    agent_id: str
    agent_name: str


@dataclass
class LoopOutcome:
    kind: str  # "completed" | "failed" | "paused" | "handoff"
    final_message: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    iterations: int = 0
    pause: PauseOutcome | None = None
    handoff: HandoffOutcome | None = None


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
        sub_agents: SubAgentToolProvider | None = None,
        # S8 (ADR 0020 §5, D67): the retriever family beside MCP and
        # sub-agents; the D68 service for the prompt injection.
        retrievers: RetrieverToolProvider | None = None,
        retrieval: KnowledgeRetrieval | None = None,
        knowledge_repo: KnowledgeRepo | None = None,
        trace_llm: bool = False,
        trace_buffer: LlmTraceBuffer | None = None,
        # S7 (ADR 0021 §1, D71): spans cross the runtime ONLY as this port —
        # None keeps the runtime byte-identical (the golden gates).
        span_sink: SpanSink | None = None,
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
        self._sub_agents = sub_agents
        self._retrievers = retrievers
        self._retrieval = retrieval
        self._knowledge_repo = knowledge_repo
        self._trace_llm = trace_llm
        self.trace_buffer = trace_buffer
        self._span_sink = span_sink
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

    def _recorder(self, ctx: ExecutionContext) -> SpanRecorder:
        """One run's span buffer (S7, D74) — no sink means a no-op recorder
        and a byte-identical runtime."""
        return SpanRecorder(
            self._span_sink,
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            tenant_id=ctx.tenant_id,
        )

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
        # S7 (D74): the run's spans — a run-local recorder (the runtime runs
        # segments concurrently), flushed at the very end so even a
        # terminal-persistence failure leaves the segment force-closed.
        spans = self._recorder(ctx)

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
            outcome = await self._run_segment(
                version,
                input,
                ctx,
                sink,
                started=RunStarted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    agent_id=agent.id,
                    agent_version_id=version.id,
                    session_id=ctx.session_id,
                    input=input,
                ),
                spans=spans,
            )
            result = await self._after_loop(ctx, sink, outcome, started_at, input)
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
        except ToolResolutionError as exc:
            # S4 (D38) / S14 (ADR 0018): the boundary fails honestly — exactly
            # one persisted terminal run.failed with error_kind="tool", the
            # resource (MCP server / sub-agent) named.
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
        # S7 (D74): the flush force-closes anything still open (the segment
        # span on an exception path, a paused iteration) with the run's own
        # final status — the safety net IS the correct closer here.
        await spans.flush(
            pending_status=status_for_run(result.status), pending_message=result.error
        )
        return result

    # --- segment (S6 commit 4: the fresh-run body, extracted) ----------------

    async def _run_segment(
        self,
        version: AgentVersion,
        input: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        started: RunStarted | None = None,
        spans: SpanRecorder | None = None,  # S7: the caller's run-local recorder
    ) -> LoopOutcome:
        """One segment of a run: model/tooling resolution (D28/D38), memory,
        prompt build, and the loop. Extracted from `run()` (S6, ADR 0015
        commit 4) so the workflow runtime can ride the same path for its
        agent nodes; `started=None` suppresses the segment's own run.started
        — a workflow run has exactly ONE run.started, emitted by its caller
        (D41). Raises on model/tooling failure; callers hold the try."""
        agent = version.snapshot
        # S7 (D74): the segment span opens here (this call IS one segment);
        # the workflow runtime reaches this seam without a recorder, so one
        # is minted when absent. Closed from the outcome below; an exception
        # leaves it open for the caller's flush to close with the run's final
        # status.
        spans = spans or self._recorder(ctx)
        spans.start(
            "run.segment",
            "run.segment",
            slot="segment",
            attributes=_segment_attrs(version.snapshot, ctx, entry="fresh"),
        )
        # Inside the segment: credential resolution is IO (S2) and can fail —
        # the failure must become a persisted terminal `model` state (D5),
        # never an escape past the runtime (which the worker would treat as
        # a claim failure and retry forever).
        client = await self._models.resolve(agent.model, principal=ctx.principal)
        if self._trace_llm:
            client = TracedModelClient(client, ctx, buffer=self.llm_trace_buffer)
        if spans.active:
            client = TracingModelClient(client, spans)
        # S4 (D38): MCP toolset resolution is the same seam, third
        # application — its failure is a persisted terminal `tool` state
        # naming the server, before any token is spent.
        tooling = await self._resolve_tooling(agent, ctx)
        try:
            memory_view = await self._load_memory(ctx, agent, client)
            # S8 (D66): the binding's datasets are retrieved ONCE per
            # segment, before the first prompt build; the rendered context
            # rides as a system message. Failure → RetrieverResolutionError
            # → run()'s catch → terminal "tool" (no new catch site, D67).
            knowledge_context = await self._retrieve_knowledge(agent, ctx, input, spans=spans)

            messages = self._prompt_engine.build(
                PromptContext(
                    agent=agent,
                    input=input,
                    variables=ctx.variables,
                    history=memory_view.history,
                    memory_summary=memory_view.summary,
                    knowledge_context=knowledge_context,
                    tools=self._bound_descriptors(agent, tooling.registry),
                    schema_in_prompt=(
                        agent.output_schema is not None
                        and client.capabilities.structured_output != "json_schema"
                    ),
                )
            )
            ctx.output_schema = agent.output_schema
            ctx.structured_mode = client.capabilities.structured_output

            if started is not None:
                await sink.append(started)
            user_message = messages[-1]
            await self._save_message(ctx, user_message)
            if memory_view.conversation_id and self._conversations is not None:
                await self._conversations.append_message(
                    memory_view.conversation_id, user_message, ctx.run_id
                )

            outcome = await self._loop(
                ctx,
                agent,
                client,
                sink,
                messages,
                memory_view.conversation_id,
                tooling=tooling,
                spans=spans,
            )
            # S7 (D74): the segment closes from the outcome; an exception
            # above left it open for run()'s flush to close with the run's
            # final status.
            _close_segment_span(spans, outcome)
            return outcome
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

        if outcome.kind in ("completed", "handoff"):
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
        spans: SpanRecorder | None = None,  # S7 (D74): the run's recorder
    ) -> LoopOutcome:
        spans = spans or SpanRecorder(None, run_id=ctx.run_id, trace_id=ctx.trace_id)
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
            # S7 (D74): one `iteration` span per loop pass — tool spans
            # parent to it; it closes at the iteration's natural end
            # (_iteration_done), at a pause, or (via the flush safety net)
            # with the run's final status.
            iter_span = spans.start(
                "iteration", "iteration", slot="iteration", attributes={"iteration": iteration}
            )
            if self._limits is not None:
                # platform-level budget (ADR 0004): the orchestrator owns limits;
                # the per-agent max_iterations cap is enforced at the loop bottom
                reason = self._limits.exceeded(ctx)
                if reason is not None:
                    spans.end_slot(
                        "iteration", status="error", status_message=f"run limit exceeded: {reason}"
                    )
                    return LoopOutcome(
                        kind="failed",
                        error=f"run limit exceeded: {reason}",
                        error_kind="max_iterations",
                        iterations=iteration,
                    )

            batch_refusals: set[str] | None = None
            auto_refusals: set[str] | None = None  # S14 (D55): child non-interactivity
            handoff: HandoffOutcome | None = None  # S14 (D56): fires after the batch
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
                    started_cursor = await sink.append(
                        IterationStarted(
                            event_id=_uuid(),
                            run_id=ctx.run_id,
                            created_at=_now(),
                            iteration=iteration,
                        )
                    )
                    if iter_span is not None:
                        # the span names the exact replay position (D74)
                        iter_span.attributes["jarvis.event.cursor"] = started_cursor
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
                    spans.end_slot(
                        "iteration",
                        status="error",
                        status_message=f"strategy {agent.strategy.type!r} raised {exc}",
                    )
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
                            await self._iteration_done(sink, ctx, iteration, spans=spans)
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
                    await self._iteration_done(sink, ctx, iteration, spans=spans)
                    return LoopOutcome(
                        kind="completed", final_message=final_text, iterations=iteration + 1
                    )

                if isinstance(step, AskHumanStep):
                    if _auto_decline(ctx):
                        # S14 (D55): a child never pauses — the ask
                        # auto-answers, the loop continues; re-asks are
                        # capped by the iteration limits at the loop top.
                        answer = Message(role="user", content=_AUTO_ANSWER)
                        messages.append(answer)
                        await self._save_message(ctx, answer)
                        if conversation_id and self._conversations is not None:
                            await self._conversations.append_message(
                                conversation_id, answer, ctx.run_id
                            )
                        await self._iteration_done(sink, ctx, iteration, spans=spans)
                        iteration += 1
                        continue
                    # S10 (ADR 0010 §3.2): the strategy wants a human answer. The
                    # assistant message is already persisted; pause the run.
                    spans.end_slot("iteration", status="ok", attributes={"paused": True})
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
                auto_refusals = None
                if gated:
                    if _auto_decline(ctx):
                        # S14 (D55): a child never pauses — the gated subset
                        # takes the decline branch with the auto-decline copy;
                        # the ungated remainder still executes.
                        auto_refusals = {call.id for call in gated}
                    else:
                        spans.end_slot("iteration", status="ok", attributes={"paused": True})
                        return await self._pause(
                            sink,
                            ctx,
                            PauseOutcome(reason="tool_approval", pending_calls=gated),
                            iteration,
                        )
                calls = step.tool_calls
                batch_refusals = None

            for call in calls:
                refusal_copy: str | None = None
                if batch_refusals and call.id in batch_refusals:
                    # S10/ADR 0011 §3: the human declined this call — the
                    # declined event records the *decision* (no
                    # started/completed; it never ran), the refusal tool
                    # message closes it in the model's context. Ungated
                    # calls in the batch still execute.
                    refusal_copy = _USER_DECLINE_COPY
                elif auto_refusals and call.id in auto_refusals:
                    # S14 (D55): the child's non-interactivity declined it —
                    # same decision event, the child's refusal copy.
                    refusal_copy = _AUTO_DECLINE_COPY
                if refusal_copy is not None:
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
                        content=refusal_copy,
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
                tool_cursor = await sink.append(
                    ToolCallStarted(
                        event_id=_uuid(),
                        run_id=ctx.run_id,
                        created_at=_now(),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                # S7 (D74): one `tool.call` span per executed call, tied to
                # the started event's cursor. Declined calls never reach
                # here — they get no span, matching the no-tool-row fact.
                tool_span = spans.start(
                    "tool.call",
                    "tool.call",
                    parent=iter_span,
                    attributes={
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "jarvis.event.cursor": tool_cursor,
                    },
                )
                binding = bindings.get(call.name)
                try:
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
                            tenant_id=ctx.tenant_id,
                        ),
                    )
                except Exception as exc:
                    spans.end(
                        tool_span, status="error", status_message=f"{type(exc).__name__}: {exc}"
                    )
                    raise
                spans.end(
                    tool_span,
                    status="error" if result.is_error else "ok",
                    status_message=result.output if result.is_error else None,
                    attributes={
                        "latency_ms": result.latency_ms,
                        "kind": result.metadata.get("kind"),
                    },
                )
                _fold_usage(ctx, result)
                if result.metadata.get("kind") == "handoff" and not result.is_error:
                    # S14 (D56): a handoff tool reported a successor. Other
                    # calls in the batch still execute and persist first; the
                    # run terminates AFTER the batch (the check below).
                    handoff = HandoffOutcome(
                        successor_run_id=str(result.metadata.get("successor_run_id") or ""),
                        agent_id=str(result.metadata.get("agent_id") or ""),
                        agent_name=str(result.metadata.get("agent_name") or ""),
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

            await self._iteration_done(sink, ctx, iteration, spans=spans)
            if handoff is not None:
                # S14 (D56): the batch is fully persisted — the predecessor
                # ends with the ORDINARY run.completed terminal, its
                # final_message naming the target and the successor run. The
                # successor runs independently on the shared trace.
                return LoopOutcome(
                    kind="handoff",
                    final_message=(
                        f"Handed off to {handoff.agent_name} (run {handoff.successor_run_id})"
                    ),
                    iterations=iteration + 1,
                    handoff=handoff,
                )
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
        spans = self._recorder(ctx)  # S7 (D74): the resumed segment records too
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
            if spans.active:
                client = TracingModelClient(client, spans)
            tooling = await self._resolve_tooling(agent, ctx)  # S4 (D38): re-resolve per segment
            result = await self._resume_segment(
                run_input, ctx, sink, client, agent, started_at, resume, tooling, spans=spans
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
        except ToolResolutionError as exc:
            # S4 (D38): same honest boundary as a fresh run — one persisted
            # terminal `tool` failure, the resource named.
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
        # S7 (D74): the same flush discipline as run().
        await spans.flush(
            pending_status=status_for_run(result.status), pending_message=result.error
        )
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
        spans: SpanRecorder | None = None,
    ) -> RunResult:
        spans = spans or self._recorder(ctx)
        spans.start(
            "run.segment",
            "run.segment",
            slot="segment",
            attributes=_segment_attrs(agent, ctx, entry="resume"),
        )
        try:
            outcome = await self._resume_loop(
                ctx, sink, client, agent, resume, tooling, run_input=run_input, spans=spans
            )
            _close_segment_span(spans, outcome)
            return await self._after_loop(ctx, sink, outcome, started_at, run_input)
        finally:
            # S4 (D38): the resumed segment's MCP connections close with the
            # segment; a later resume re-resolves and reconnects.
            await tooling.aclose()

    async def resume_segment(
        self,
        version: AgentVersion,
        run_id: str,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        resume: ResumeRequest,
        *,
        open_iteration: int | None = None,
    ) -> LoopOutcome:
        """The workflow runtime's resume seam (ADR 0015 §5, the S10
        composition): everything `resume()` does between the row re-seed and
        the loop — usage re-seed from the run row so limits span the whole
        chain, client + tooling resolution (D28/D38) — returning the outcome
        WITHOUT terminal events or row writes. Raises on model/tooling
        failure; callers hold the try. `open_iteration` overrides the
        replay-based re-seed: a workflow run's event log spans nodes, and the
        workflow runtime counts the PAUSED node's own iterations."""
        agent = version.snapshot
        ctx.temperature = agent.temperature
        self._live_tokens[run_id] = ctx
        spans = self._recorder(ctx)  # S7 (D74)
        spans.start(
            "run.segment",
            "run.segment",
            slot="segment",
            attributes=_segment_attrs(agent, ctx, entry="resume"),
        )
        if self._executions is not None:
            row = await self._executions.get(run_id)
            if row is not None:
                ctx.usage = row.total_usage.model_copy()
        client = await self._models.resolve(agent.model, principal=ctx.principal)
        if self._trace_llm:
            client = TracedModelClient(client, ctx, buffer=self.llm_trace_buffer)
        if spans.active:
            client = TracingModelClient(client, spans)
        tooling = await self._resolve_tooling(agent, ctx)  # S4 (D38): re-resolve per segment
        try:
            outcome = await self._resume_loop(
                ctx, sink, client, agent, resume, tooling, open_iteration, spans=spans
            )
            _close_segment_span(spans, outcome)
            return outcome
        finally:
            # S4 (D38): the resumed segment's MCP connections close with the
            # segment; a later resume re-resolves and reconnects.
            await tooling.aclose()

    async def _resume_loop(
        self,
        ctx: ExecutionContext,
        sink: InProcessEventSink,
        client: ModelClient,
        agent: AgentDefinition,
        resume: ResumeRequest,
        tooling: McpTooling,
        open_iteration: int | None = None,
        run_input: str = "",
        spans: SpanRecorder | None = None,  # S7 (D74)
    ) -> LoopOutcome:
        """The resumed segment's loop entry: counter re-seed from the durable
        log, message rebuild, batch/answer entry decision, then the loop.
        No terminals, no row writes — the callers own those."""
        # Re-seed the chain's counters from the durable log: the open
        # iteration is the last one that STARTED (no IterationCompleted was
        # emitted at the pause) and the pause event carries the gated batch.
        pause_event: RunAwaitingInput | None = None
        iteration_starts = 0
        if open_iteration is None:
            if self._executions is not None:
                async for event in self._executions.list_events(ctx.run_id):
                    if isinstance(event, IterationStarted):
                        iteration_starts += 1
                    elif isinstance(event, RunAwaitingInput):
                        pause_event = event
            open_iteration = max(iteration_starts - 1, 0)
        elif self._executions is not None:
            # the caller already knows the paused node's iteration — still
            # scan for the pause frame (the gated batch rides it)
            async for event in self._executions.list_events(ctx.run_id):
                if isinstance(event, RunAwaitingInput):
                    pause_event = event
        ctx.iteration = open_iteration

        # S8 (D66): the resumed segment retrieves against the run's
        # ORIGINAL input (the row carries it) — same per-segment contract.
        knowledge_context = await self._retrieve_knowledge(agent, ctx, None, spans=spans)

        conversation_id = await self._resume_conversation(ctx, agent)
        messages = await self._rebuild_messages(
            agent,
            ctx,
            client,
            ctx.run_id,
            conversation_id,
            tooling.registry,
            knowledge_context=knowledge_context,
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
            spans=spans,
        )
        return outcome

    async def _retrieve_knowledge(
        self,
        agent: AgentDefinition,
        ctx: ExecutionContext,
        query: str | None,
        spans: SpanRecorder | None = None,
    ) -> str | None:
        """The D66 knowledge injection: the binding's datasets resolve
        tenant-scoped ONCE per segment, the query embeds, the rendered
        `[doc name] content` context rides into the prompt as a system
        message. No binding → None (byte-identical prompt). Failures raise
        RetrieverResolutionError — a ToolResolutionError — so run()/resume()'s
        EXISTING catch persists the terminal `tool` state (D67: no new
        catch site); the caller maps the same failure onto 502."""
        binding = agent.knowledge
        if binding is None:
            return None
        # S7 (D74): one `knowledge.retrieve` span per segment — resolution,
        # embed, search, and render are one retrieval.
        retrieve_span = (
            spans.start(
                "knowledge.retrieve",
                "knowledge.retrieve",
                attributes={"datasets": ",".join(binding.datasets), "top_k": binding.top_k},
            )
            if spans is not None
            else None
        )
        try:
            if self._retrieval is None or self._knowledge_repo is None:
                raise RetrieverResolutionError(
                    ", ".join(binding.datasets),
                    "no knowledge retrieval is wired into this runtime",
                )
            if query is None:
                # A resumed segment retrieves against the run's ORIGINAL
                # input (the row carries it) — same per-segment contract.
                row = (
                    await self._executions.get(ctx.run_id) if self._executions is not None else None
                )
                query = row.input if row is not None else ""
            datasets: list[KnowledgeDataset] = []
            for dataset_id in binding.datasets:
                dataset = await self._knowledge_repo.get_dataset(
                    dataset_id, tenant_id=ctx.tenant_id or ""
                )
                if dataset is None:
                    # D29: a foreign (or deleted) dataset reads as absent — the
                    # binding names an unrunnable resource, honestly terminal.
                    raise RetrieverResolutionError(
                        f"retriever__{dataset_id}", "dataset not found in this tenant"
                    )
                datasets.append(dataset)
            context = await self._retrieval.retrieve(
                datasets,
                query,
                tenant_id=ctx.tenant_id or "",
                principal=ctx.principal,
                top_k=binding.top_k,
                min_score=binding.min_score,
            )
            if spans is not None:
                spans.end(retrieve_span, attributes={"chars": len(context)})
            return context
        except Exception as exc:
            if spans is not None:
                spans.end(
                    retrieve_span, status="error", status_message=f"{type(exc).__name__}: {exc}"
                )
            raise

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
        knowledge_context: str | None = None,
    ) -> list[Message]:
        """Reconstruct the model's context for a resumed segment: the system
        prompt plus the run transcript. With memory on, the conversation
        history is the superset (prior sessions folded in with this run's
        messages — the loop appends to both), reduced to the SAME view a
        fresh segment sees: existing summary + window, read-only (S12,
        D45 — no compaction on resume; fixes the S6-documented v1
        approximation of feeding the FULL conversation). Otherwise the
        run's own persisted transcript is exactly what the original
        context was. The system prompt renders against the SEGMENT's
        registry (S4: the fresh segment's resolved tools must match what
        it can execute)."""
        summary: str | None = None
        if conversation_id and self._conversations is not None:
            view = await ConversationMemory(self._conversations).rebuild_view(
                agent, ctx, conversation_id
            )
            transcript = view.history
            summary = view.summary
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
                knowledge_context=knowledge_context,
            )
        )
        messages = [Message(role="system", content=system)] if system else []
        if summary:
            messages.append(
                Message(role="system", content="Summary of the earlier conversation:\n" + summary)
            )
        # S8 (D66): the retrieval's system message mirrors build()'s seam —
        # a resumed segment's context matches what a fresh segment saw.
        if knowledge_context:
            messages.append(
                Message(
                    role="system",
                    content="Relevant knowledge base excerpts:\n\n" + knowledge_context,
                )
            )
        messages.extend(transcript)
        return messages

    # --- memory -------------------------------------------------------------

    async def _load_memory(
        self, ctx: ExecutionContext, agent: AgentDefinition, client: ModelClient
    ) -> MemoryView:
        """The segment's memory view (S12, D45): full history + rolling
        summary, with compaction when the summarize strategy is on. Window
        agents (and every pre-S12 snapshot) get exactly the old
        load-history behavior — byte-identical."""
        if self._conversations is None or not agent.memory.enabled or not ctx.session_id:
            return MemoryView(history=[])
        memory = ConversationMemory(self._conversations)
        return await memory.load(agent, ctx, client=client)

    # --- persistence helpers --------------------------------------------------

    async def _save_message(self, ctx: ExecutionContext, message: Message) -> None:
        if self._executions is not None:
            await self._executions.save_message(ctx.run_id, message)

    async def _iteration_done(
        self,
        sink: InProcessEventSink,
        ctx: ExecutionContext,
        iteration: int,
        spans: SpanRecorder | None = None,
    ) -> None:
        # S7 (D74): the natural iteration end closes the iteration span.
        if spans is not None:
            spans.end_slot("iteration", status="ok")
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
        today; any resolution failure raises ToolResolutionError (caught in
        run()/resume()'s try — D28 pattern). S14 (ADR 0018 §2): the sub-agent
        family (`sub__*` AND `handoff__*`) resolves through the sub-agent
        provider and merges into ONE registry view — one ToolRuntime, one
        aclose for mcp__ + family together; no family bindings leaves the
        MCP path byte-identical. S8 (D67): the `retriever__<dataset>`
        family joins the merge the same way."""
        bindings = agent.enabled_tools()
        sub_bindings = [b for b in bindings if b.enabled and b.name.startswith(FAMILY_PREFIXES)]
        # S8 (ADR 0020 §5, D67): the retriever family merges the same way —
        # one registry view, one ToolRuntime, one aclose for all families.
        retriever_bindings = [b for b in bindings if b.enabled and is_retriever_binding(b.name)]
        if not sub_bindings and not retriever_bindings:
            if self._mcp is None:
                return self._default_tooling()
            return await self._mcp.resolve(
                bindings, tenant_id=ctx.tenant_id, principal=ctx.principal
            )
        if retriever_bindings and self._retrievers is None:
            raise RetrieverResolutionError(
                retriever_bindings[0].name, "no retriever provider is wired into this runtime"
            )
        mcp_tooling = (
            await self._mcp.resolve(bindings, tenant_id=ctx.tenant_id, principal=ctx.principal)
            if self._mcp is not None
            else self._default_tooling()
        )
        # S8: with the third family in the merge, a retriever-only agent
        # reaches this block with EMPTY sub_bindings — the provider is
        # optional per family, so the guard lives inside the bindings'
        # block (an unconditional raise here would IndexError on
        # sub_bindings[0] and escape as a non-ToolResolutionError).
        sub_tooling: SubAgentTooling | None = None
        sub_provider = self._sub_agents
        try:
            if sub_bindings:
                if sub_provider is None:
                    raise SubAgentResolutionError(
                        sub_bindings[0].name, "no sub-agent provider is wired into this runtime"
                    )
                sub_tooling = await sub_provider.resolve(sub_bindings, ctx)
        except Exception:
            await mcp_tooling.aclose()
            raise
        retriever_tooling: McpTooling | None = None
        try:
            retriever_provider = self._retrievers
            if retriever_bindings:
                if retriever_provider is None:
                    raise RetrieverResolutionError(
                        retriever_bindings[0].name,
                        "no retriever provider is wired into this runtime",
                    )
                retriever_tooling = await retriever_provider.resolve(retriever_bindings, agent, ctx)
        except Exception:
            await mcp_tooling.aclose()
            if sub_tooling is not None:
                await sub_tooling.aclose()
            raise
        view = InMemoryToolRegistry()
        for descriptor in mcp_tooling.registry.descriptors():
            view.register(mcp_tooling.registry.get(descriptor.name))
        if sub_tooling is not None:
            for tool in sub_tooling.tools:
                view.register(tool)
        if retriever_tooling is not None:
            for descriptor in retriever_tooling.registry.descriptors():
                view.register(retriever_tooling.registry.get(descriptor.name))
        closes = [mcp_tooling.aclose]
        if sub_tooling is not None:
            closes.append(sub_tooling.aclose)
        if retriever_tooling is not None:
            closes.append(retriever_tooling.aclose)
        return McpTooling(
            registry=view,
            tool_runtime=ToolRuntime(view),
            aclose=_merged_close(*closes),
        )

    def _default_tooling(self) -> McpTooling:
        return McpTooling(
            registry=self._tools, tool_runtime=self._tool_runtime, aclose=_no_op_close
        )


def _auto_decline(ctx: ExecutionContext) -> bool:
    """S14 (D55): a sub-agent child never pauses — its queue message rides
    `auto_decline_approvals: true` in metadata, and the worker threads
    message.metadata into ctx.metadata."""
    return bool(ctx.metadata.get("auto_decline_approvals"))


def _fold_usage(ctx: ExecutionContext, result: ToolResult) -> None:
    """S14 (D54): a tool-reported usage (the sub-agent family reports the
    child's RunCompleted.total_usage in metadata['usage']) folds into the
    run's total — `RunLimits.exceeded` at the next iteration top clamps the
    whole tree. A malformed report is advisory — it never fails the call."""
    report = result.metadata.get("usage")
    if not isinstance(report, dict):
        return
    try:
        reported = Usage.model_validate(report)
    except ValidationError:
        return
    ctx.usage = ctx.usage.plus(reported)


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


def _merged_close(*closes: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    """The merged toolset's aclose: every family's close runs exactly once,
    first-error tolerant (the MCP _Close pattern; variadic since S8's
    retriever family joined — S14 two, S8 three)."""

    async def _close() -> None:
        errors: list[Exception] = []
        for close in closes:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 — never block the other close
                errors.append(exc)
        if errors:
            raise errors[0]

    return _close


def _segment_attrs(agent: AgentDefinition, ctx: ExecutionContext, *, entry: str) -> dict[str, Any]:
    """The run.segment span's open attributes (S7, ADR 0021 §3) — identity
    and provenance the run already carries; None values drop out."""
    trigger = ctx.metadata.get("trigger")
    attrs: dict[str, Any] = {
        "agent_id": ctx.agent_id,
        "agent_version_id": ctx.agent_version_id,
        "entry": entry,
        "parent_run_id": ctx.parent_run_id,
        "session_id": ctx.session_id,
        "tenant_id": ctx.tenant_id,
    }
    if isinstance(trigger, dict):
        attrs["trigger_id"] = trigger.get("trigger_id")
        attrs["trigger_kind"] = trigger.get("kind")
        attrs["trigger_name"] = trigger.get("name")
    return attrs


def _close_segment_span(spans: SpanRecorder, outcome: LoopOutcome) -> None:
    """Close the segment slot from the loop's outcome (S7, D74): a failure
    is an error span naming the kind, a pause closes ok with `paused`, and
    a completion (or a handoff — the predecessor ended ordinarily) closes
    ok. An exception path leaves the slot open on purpose — run()'s flush
    closes it with the run's own final status."""
    if outcome.kind == "failed":
        spans.end_slot(
            "segment",
            status="error",
            status_message=outcome.error,
            attributes={"error_kind": outcome.error_kind},
        )
    elif outcome.kind == "paused":
        spans.end_slot(
            "segment",
            status="ok",
            attributes={
                "paused": True,
                "pause_reason": outcome.pause.reason if outcome.pause else None,
            },
        )
    else:  # completed | handoff
        spans.end_slot("segment", status="ok")


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["AgentRuntime"]
