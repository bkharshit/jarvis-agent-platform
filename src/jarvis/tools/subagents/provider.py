"""The S14 sub-agent tool family (ADR 0018 §2/§5) — `sub__<agent>` bindings
resolved per segment into ordinary BaseTools that enqueue ordinary queue
runs (the eval composition, D52) and block on the child's event stream.

Mirrors `tools/mcp/provider.py`: eager per-segment resolution with honest
`SubAgentResolutionError` failures (a persisted terminal `tool` failure —
the D38 pattern, seventh application), descriptor minting, and run-scoped
values captured on the tool instances at resolve() time. Name is
display-only; identity comes from `config.agent_id` — agent names are
arbitrary strings, never parsed from the `sub__` prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from jarvis.domain.agent import AgentVersion, ToolBinding
from jarvis.domain.events import ExecutionEvent, RunCancelled, RunCompleted, RunFailed
from jarvis.domain.execution import ExecutionCancelled, ExecutionContext
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.runtime.child_runs import QueuedRunWriter, child_message, enqueue_child_run
from jarvis.tools.base import BaseTool
from jarvis.tools.mcp.errors import SubAgentResolutionError

logger = logging.getLogger("jarvis.tools.subagents")

SUB_PREFIX = "sub__"


class SubAgentError(Exception):
    """A delegation failed honestly at CALL time — the child failed, its
    stream ended without a terminal, or a bound was hit (depth cap, parent
    deadline). ToolRuntime converts this into an is_error ToolResult the
    model can recover from — never a terminal (D52/D55)."""


class _ChildCancelled(ExecutionCancelled):
    """The CHILD's run.cancelled observed on its stream — distinguishable
    from the parent's own cancellation so the handler re-raises it without
    a (pointless) cancel request for an already-terminal run."""


class SubAgentRepo(Protocol):
    """Structural view of the agent repo: tenant-scoped latest published
    version (D29 — a foreign sub-agent reads as absent)."""

    async def latest_version(
        self, agent_id: str, *, tenant_id: str | None = None
    ) -> AgentVersion | None: ...


class ChildRunQueue(Protocol):
    """The one queue call a blocked parent needs: cross-process cancel."""

    async def request_cancel(self, run_id: str, reason: str) -> None: ...


class ChildEventStream(Protocol):
    """Structural view of the read side the tool blocks on
    (`PgEventStream.subscribe` — the EventStream port plus the
    end_on_pause_status flag the S10 resume semantics added)."""

    def subscribe(
        self,
        run_id: str,
        last_cursor: int | None = None,
        *,
        end_on_pause_status: bool = True,
    ) -> AsyncGenerator[tuple[int, ExecutionEvent], None]:
        """An async generator — the tool closes it explicitly (the finally
        keeps the subscriber queue from leaking on every early exit)."""


@dataclass
class SubAgentTooling:
    """The per-segment sub-agent toolset — plain tools the runtime merges
    into the segment's registry view (one registry, one ToolRuntime, one
    aclose for mcp__ + sub__ together, D51)."""

    tools: list[BaseTool]
    aclose: Callable[[], Awaitable[None]]


class SubAgentTool(BaseTool):
    """One bound sub-agent as an ordinary BaseTool. The delegation is ONE
    parent iteration: enqueue an ordinary child run, block on its stream to
    the terminal, return the child's final message. All bounded endings are
    non-terminal — the parent's own cancellation/deadline machinery decides
    the parent's fate (D52)."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        *,
        agent_id: str,
        version_id: str,
        parent_ctx: ExecutionContext,
        executions: QueuedRunWriter,
        queue: ChildRunQueue,
        stream: ChildEventStream,
        max_depth: int,
        parent_depth: int,
    ) -> None:
        super().__init__(descriptor)
        self._agent_id = agent_id
        self._version_id = version_id
        self._parent_ctx = parent_ctx
        self._executions = executions
        self._queue = queue
        self._stream = stream
        self._max_depth = max_depth
        self._parent_depth = parent_depth

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        task = str(arguments.get("task") or "")
        child_depth = self._parent_depth + 1
        if child_depth > self._max_depth:
            # Over-cap spawn is a recoverable error ToolResult, never a
            # terminal (D55) — the model sees the refusal and proceeds.
            raise SubAgentError(
                f"sub-agent depth cap ({self._max_depth}) reached — "
                f"delegation to depth {child_depth} refused"
            )
        message = child_message(
            agent_id=self._agent_id,
            agent_version_id=self._version_id,
            input=task,
            tenant_id=self._parent_ctx.tenant_id,
            principal=self._parent_ctx.principal,
            # D52: a child gets no session — cross-agent sessions share
            # nothing, and the child's memory path is its own decision.
            session_id=None,
            user_id=self._parent_ctx.user_id,
            trace_id=self._parent_ctx.trace_id,
            deadline=self._parent_ctx.deadline,
            parent_run_id=self._parent_ctx.run_id,
            metadata={
                "sub_agent": {"depth": child_depth},
                # D55: children never pause — approvals auto-decline, asks
                # auto-answer, and the runtime's branches key off this flag.
                "auto_decline_approvals": True,
            },
        )
        child_run_id = await enqueue_child_run(executions=self._executions, message=message)
        # The parent's remaining budget bounds the whole wait (D52: REQUIRED
        # — the sweeper never reaps pending rows, so without this bound a
        # stuck child blocks the parent forever). None = unbounded, exactly
        # like the ToolRuntime `timeout <= 0` sentinel.
        timeout = (
            None
            if self._parent_ctx.deadline is None
            else max(0.0, (self._parent_ctx.deadline - datetime.now(UTC)).total_seconds())
        )
        try:
            if timeout is None:
                return await self._await_child(child_run_id, context)
            return await asyncio.wait_for(self._await_child(child_run_id, context), timeout=timeout)
        except TimeoutError:
            await self._cancel_child(child_run_id)
            raise SubAgentError(
                f"child run {child_run_id} aborted: the parent's deadline expired while waiting"
            ) from None
        # ExecutionCancelled (cancel observed / child cancelled) propagates
        # as-is — _await_child has already requested the child's cancel.

    async def _await_child(self, child_run_id: str, context: ToolContext) -> str:
        """Block on the child's event stream to exactly one terminal.
        Cancellation is observed between batches via the parent's token
        (ToolContext carries it directly) — up to one fallback poll late."""
        stream = self._stream.subscribe(child_run_id)
        try:
            try:
                async for _cursor, event in stream:
                    context.raise_if_cancelled()
                    self._parent_ctx.check_limits()
                    if isinstance(event, RunCompleted):
                        # D54: the child's usage rides the terminal event
                        # (never the row) — reported to the parent's runtime
                        # through the ToolContext channel, which the
                        # ToolRuntime merges into ToolResult.metadata and the
                        # loop folds into ctx.usage.
                        context.result_metadata["usage"] = event.total_usage.model_dump()
                        return event.final_message
                    if isinstance(event, RunFailed):
                        raise SubAgentError(f"child run {child_run_id} failed: {event.error}")
                    if isinstance(event, RunCancelled):
                        # D54: cancelling the child directly propagates
                        # honestly — the parent re-raises and ends
                        # run.cancelled too.
                        raise _ChildCancelled(event.reason)
            except _ChildCancelled:
                raise  # the child is already terminal — nothing to request
            except ExecutionCancelled as exc:
                await self._cancel_child(child_run_id)
                if self._parent_ctx.cancel.triggered:
                    raise  # a real cancellation — the parent's terminal is run.cancelled
                # Deadline fired between batches (not a user cancel): an
                # error result, not a re-raise (D52) — the parent's own next
                # checkpoint turns the deadline into its terminal.
                raise SubAgentError(f"child run {child_run_id} aborted: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()
        # Stream ended without a terminal: a pause frame (children are
        # non-interactive — D55; this is defense-in-depth until the
        # auto-decline branch) or a vanished child. Bounded ending #2.
        await self._cancel_child(child_run_id)
        raise SubAgentError(f"child run {child_run_id} ended without a terminal event")

    async def _cancel_child(self, child_run_id: str) -> None:
        try:
            await self._queue.request_cancel(child_run_id, "parent stopped waiting")
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort
            logger.warning("request_cancel failed for child %s: %s", child_run_id, exc)


async def _no_op_close() -> None: ...


class SubAgentToolProvider:
    """The runtime's sub-agent seam (D51). Resolution is eager per segment:
    each bound `sub__*` agent's latest published version is resolved (tenant
    scoped — D29) and the tool minted with the run-scoped values captured.
    `agents` also serves the commit-6 pin upgrade (`get_version_by_id`)."""

    def __init__(
        self,
        agents: SubAgentRepo,
        executions: QueuedRunWriter,
        queue: ChildRunQueue,
        stream: ChildEventStream,
        *,
        max_depth: int = 2,
    ) -> None:
        self._agents = agents
        self._executions = executions
        self._queue = queue
        self._stream = stream
        self._max_depth = max_depth

    async def resolve(self, bindings: list[ToolBinding], ctx: ExecutionContext) -> SubAgentTooling:
        """Mint one SubAgentTool per enabled `sub__*` binding. No such
        bindings → an empty toolset (the runtime keeps its byte-identical
        path). Resolution failures raise SubAgentResolutionError naming the
        binding — the runtime converts them into the persisted terminal."""
        metadata = ctx.metadata.get("sub_agent")
        parent_depth = int(metadata.get("depth", 0)) if isinstance(metadata, dict) else 0
        tools: list[BaseTool] = []
        for binding in bindings:
            if not binding.enabled or not binding.name.startswith(SUB_PREFIX):
                continue
            agent_id = binding.config.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id:
                raise SubAgentResolutionError(
                    binding.name,
                    "binding config carries no agent_id (the pin is stamped by PATCH /v1/agents)",
                )
            # D29: the tenant-scoped read — a foreign agent reads as absent,
            # exactly like a missing one.
            version = await self._agents.latest_version(agent_id, tenant_id=ctx.tenant_id)
            if version is None:
                raise SubAgentResolutionError(
                    binding.name,
                    f"agent {agent_id!r} not found (or invisible to this tenant) "
                    "with no published version",
                )
            tools.append(
                SubAgentTool(
                    self._mint_descriptor(binding.name, version),
                    agent_id=agent_id,
                    version_id=version.id,
                    parent_ctx=ctx,
                    executions=self._executions,
                    queue=self._queue,
                    stream=self._stream,
                    max_depth=self._max_depth,
                    parent_depth=parent_depth,
                )
            )
        return SubAgentTooling(tools=tools, aclose=_no_op_close)

    @staticmethod
    def _mint_descriptor(name: str, version: AgentVersion) -> ToolDescriptor:
        snapshot = version.snapshot
        description = (
            f"Delegate a task to sub-agent {snapshot.name!r}."
            + (f" {snapshot.description}" if snapshot.description else "")
            + " Provide the complete task text; the sub-agent runs to"
            " completion and returns only its final answer."
        )
        return ToolDescriptor(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete task for the sub-agent, with all context it needs"
                        ),
                    }
                },
                "required": ["task"],
            },
            # D55: a delegation blocks for minutes (unbounded sentinel) and
            # never gates on a human — the child auto-declines approvals.
            annotations={"requires_approval": False, "timeout": -1},
        )


__all__ = [
    "SUB_PREFIX",
    "ChildEventStream",
    "ChildRunQueue",
    "SubAgentError",
    "SubAgentRepo",
    "SubAgentTool",
    "SubAgentToolProvider",
    "SubAgentTooling",
]
