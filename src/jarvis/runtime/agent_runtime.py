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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import jsonschema

from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.events import (
    EventSequenceError,
    IterationCompleted,
    IterationStarted,
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
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.events.bus import InProcessEventBus, InProcessEventSink
from jarvis.models.errors import ModelAbortedError, ModelError
from jarvis.ports.model import ModelClient, ModelProviderFactory
from jarvis.ports.repository import ConversationRepo, ExecutionRepo
from jarvis.ports.strategy import FinishStep, StepOutcome, StrategyRegistry, ToolCallsStep
from jarvis.ports.tools import ToolRegistry
from jarvis.prompt.engine import PromptContext, PromptEngine
from jarvis.runtime.limits import RunLimits
from jarvis.tools.runtime import ToolRuntime

ErrorKind = Literal["max_iterations", "timeout", "model", "tool", "output_schema"]


@dataclass
class LoopOutcome:
    kind: str  # "completed" | "failed"
    final_message: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    iterations: int = 0


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
        self._live_tokens: dict[str, ExecutionContext] = {}

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
        client = self._models.resolve(agent.model, principal=ctx.principal)

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
                    session_id=ctx.session_id,
                    trace_id=ctx.trace_id,
                    started_at=started_at,
                )
            )

        try:
            result = await self._execute(version, input, ctx, sink, client, agent, started_at)
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

        if self._executions is not None:
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
    ) -> RunResult:
        history, conversation_id = await self._load_memory(ctx, agent)

        messages = self._prompt_engine.build(
            PromptContext(
                agent=agent,
                input=input,
                variables=ctx.variables,
                history=history,
                tools=self._bound_descriptors(agent),
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

        outcome = await self._loop(ctx, agent, client, sink, messages, conversation_id)

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
                result_input=input,
            )
        return await self._terminal_failed(
            ctx,
            sink,
            outcome.error or "unknown error",
            outcome.error_kind or "model",
            started_at,
            input,
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
    ) -> LoopOutcome:
        strategy = self._strategies.resolve(agent.strategy)
        descriptors = self._bound_descriptors(agent)
        bindings = {binding.name: binding for binding in agent.enabled_tools()}
        repair_attempted = False
        iteration = 0

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
            await sink.append(
                IterationStarted(
                    event_id=_uuid(),
                    run_id=ctx.run_id,
                    created_at=_now(),
                    iteration=iteration,
                )
            )

            step: StepOutcome = await strategy.step(ctx, list(messages), client, descriptors, sink)
            messages.append(step.assistant_message)
            for extra in step.messages:
                messages.append(extra)
            await self._save_message(ctx, step.assistant_message)
            for extra in step.messages:
                await self._save_message(ctx, extra)
            if conversation_id:
                for message in [step.assistant_message, *step.messages]:
                    await self._conversations.append_message(conversation_id, message, ctx.run_id)  # type: ignore[union-attr]

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

            assert isinstance(step, ToolCallsStep)
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
                result = await self._tool_runtime.execute(
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

    # --- memory -------------------------------------------------------------

    async def _load_memory(
        self, ctx: ExecutionContext, agent: AgentDefinition
    ) -> tuple[list[Message], str | None]:
        if not agent.memory.enabled or not ctx.session_id or self._conversations is None:
            return [], None
        conversation_id = await self._conversations.get_or_create(agent.id, ctx.session_id)
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

    def _bound_descriptors(self, agent: AgentDefinition) -> list[ToolDescriptor]:
        descriptors = []
        for binding in agent.enabled_tools():
            try:
                descriptors.append(self._tools.get(binding.name).descriptor)
            except KeyError:
                continue  # unknown binding names are skipped, not fatal
        return descriptors


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


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["AgentRuntime"]
