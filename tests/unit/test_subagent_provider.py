"""S14 commit 4 — the sub-agent tool family (ADR 0018 §2/§5, D51/D52).

Provider resolution failures, descriptor minting, the child-message shape,
and every bounded wait ending — all with fake repo/queue/stream doubles; the
ToolRuntime envelope itself is already regression-gated (test_tool_runtime).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.events import ExecutionEvent, RunCancelled, RunCompleted, RunFailed, RunStarted
from jarvis.domain.execution import ExecutionCancelled, ExecutionContext
from jarvis.domain.message import ToolCall, Usage
from jarvis.ports.queue import RunQueueMessage
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.mcp.errors import SubAgentResolutionError, ToolResolutionError
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from jarvis.tools.subagents.provider import SubAgentError, SubAgentToolProvider

# --- the doubles ---------------------------------------------------------------


class FakeAgents:
    def __init__(self, versions: dict[str, AgentVersion]):
        self._versions = versions
        self.lookups: list[tuple[str, str | None]] = []

    async def latest_version(self, agent_id: str, *, tenant_id: str | None = None):
        self.lookups.append((agent_id, tenant_id))
        return self._versions.get(agent_id)


class FakeExecutions:
    def __init__(self) -> None:
        self.written: list[tuple[Any, RunQueueMessage]] = []

    async def create_queued_run(self, result, message) -> None:
        self.written.append((result, message))


class FakeQueue:
    def __init__(self) -> None:
        self.cancels: list[tuple[str, str]] = []

    async def request_cancel(self, run_id: str, reason: str) -> None:
        self.cancels.append((run_id, reason))


class FakeStream:
    """Scripted subscription: yields the scripted events to ANY subscriber
    (the child's run_id is generated inside the tool), then ends.
    `hung=True` yields nothing forever (a stuck child)."""

    def __init__(self, events: list[ExecutionEvent] | None = None, *, hung=False):
        self._events = events or []
        self._hung = hung
        self.subscribed: list[str] = []

    def subscribe(self, run_id: str, last_cursor=None, *, end_on_pause_status=True):
        self.subscribed.append(run_id)
        return self._gen()

    async def _gen(self):
        for cursor, event in enumerate(self._events, start=1):
            yield cursor, event
        if self._hung:
            await asyncio.Event().wait()  # never set — simulates a stuck child


def _target_version() -> AgentVersion:
    agent = AgentDefinition(
        id="worker",
        name="Worker",
        description="does the grunt work",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
    )
    return AgentVersion(id=f"ver-{uuid4().hex[:8]}", agent_id="worker", version=1, snapshot=agent)


def _parent_ctx(run_id: str = "run-parent", **kw) -> ExecutionContext:
    base: dict[str, Any] = dict(
        run_id=run_id,
        agent_id="supervisor",
        agent_version_id="ver-sup",
        tenant_id="t1",
        trace_id="trace-1",
        deadline=None,
    )
    base.update(kw)
    return ExecutionContext(**base)


def _event(cls, run_id: str, **kw) -> ExecutionEvent:
    base: dict[str, Any] = dict(event_id=str(uuid4()), run_id=run_id, created_at=datetime.now(UTC))
    base.update(kw)
    return cls(**base)  # type: ignore[arg-type]


def _provider(
    agents=None, executions=None, queue=None, stream=None, *, max_depth=2
) -> SubAgentToolProvider:
    return SubAgentToolProvider(
        agents or FakeAgents({}),
        executions or FakeExecutions(),
        queue or FakeQueue(),
        stream or FakeStream(),
        max_depth=max_depth,
    )


_BINDING = ToolBinding(name="sub__worker", config={"agent_id": "worker"})


# --- provider resolution --------------------------------------------------------


class TestProviderResolution:
    async def test_no_sub_bindings_yields_empty_toolset(self):
        tooling = await _provider().resolve([ToolBinding(name="calculator")], _parent_ctx())
        assert tooling.tools == []

    async def test_disabled_binding_is_skipped(self):
        tooling = await _provider().resolve(
            [ToolBinding(name="sub__worker", enabled=False, config={"agent_id": "worker"})],
            _parent_ctx(),
        )
        assert tooling.tools == []

    async def test_binding_without_agent_id_raises_naming_the_binding(self):
        with pytest.raises(SubAgentResolutionError) as excinfo:
            await _provider().resolve([ToolBinding(name="sub__worker")], _parent_ctx())
        assert "sub__worker" in str(excinfo.value)

    async def test_unpublished_or_foreign_agent_raises(self):
        # D29: a foreign sub-agent reads as absent — latest_version is
        # tenant-scoped, so the provider's error is the same for both.
        agents = FakeAgents({})
        with pytest.raises(SubAgentResolutionError):
            await _provider(agents).resolve([_BINDING], _parent_ctx())
        assert agents.lookups == [("worker", "t1")]

    async def test_resolution_failure_is_under_the_shared_base(self):
        assert issubclass(SubAgentResolutionError, ToolResolutionError)

    async def test_minted_descriptor_shape(self):
        version = _target_version()
        tooling = await _provider(FakeAgents({"worker": version})).resolve(
            [_BINDING], _parent_ctx()
        )
        assert len(tooling.tools) == 1
        descriptor = tooling.tools[0].descriptor
        assert descriptor.name == "sub__worker"
        assert "Worker" in descriptor.description
        assert descriptor.parameters["required"] == ["task"]
        # D55: unbounded wait + never approval-gated
        assert descriptor.annotations == {"requires_approval": False, "timeout": -1}

    async def test_resolution_tenant_scopes_the_lookup(self):
        version = _target_version()
        agents = FakeAgents({"worker": version})
        await _provider(agents).resolve([_BINDING], _parent_ctx())
        assert agents.lookups == [("worker", "t1")]

    async def test_depth_captured_from_context_metadata(self):
        version = _target_version()
        tooling = await _provider(FakeAgents({"worker": version})).resolve(
            [_BINDING], _parent_ctx(metadata={"sub_agent": {"depth": 1}})
        )
        assert tooling.tools[0]._parent_depth == 1


# --- the tool call --------------------------------------------------------------


class TestSubAgentToolCall:
    async def test_completed_child_returns_final_message(self):
        version = _target_version()
        executions, queue = FakeExecutions(), FakeQueue()
        stream = FakeStream(
            [
                _event(RunStarted, "child-1", agent_id="worker", agent_version_id="v"),
                _event(
                    RunCompleted,
                    "child-1",
                    final_message="done!",
                    total_usage=Usage(),
                    iterations=3,
                ),
            ]
        )
        tooling = await _provider(
            FakeAgents({"worker": version}), executions, queue, stream
        ).resolve([_BINDING], _parent_ctx())
        tool = tooling.tools[0]
        output = await tool.execute({"task": "go"}, _tool_context())
        assert output == "done!"
        # The child is an ordinary queue run sharing the parent's linkage (D52)
        result, message = executions.written[0]
        assert result.status == "queued"
        assert message.input == "go"
        assert message.parent_run_id == "run-parent"
        assert message.trace_id == "trace-1"
        assert message.tenant_id == "t1"
        assert message.session_id is None
        assert message.metadata == {
            "sub_agent": {"depth": 1},
            "auto_decline_approvals": True,
        }
        assert message.agent_version_id == version.id
        assert stream.subscribed == [message.run_id]
        assert queue.cancels == []

    async def test_completed_child_reports_usage_through_the_context(self):
        """D54: the child's terminal-event usage rides ToolContext.
        result_metadata — the ToolRuntime merges it into ToolResult.metadata
        and the parent's loop folds it into ctx.usage."""
        version = _target_version()
        stream = FakeStream(
            [
                _event(
                    RunCompleted,
                    "child-1",
                    final_message="done!",
                    total_usage=Usage(input_tokens=120, output_tokens=80),
                    iterations=2,
                )
            ]
        )
        tooling = await _provider(FakeAgents({"worker": version}), stream=stream).resolve(
            [_BINDING], _parent_ctx()
        )
        ctx = _tool_context()
        await tooling.tools[0].execute({"task": "go"}, ctx)
        assert ctx.result_metadata["usage"] == {
            "input_tokens": 120,
            "output_tokens": 80,
            "extra": {},
        }

    async def test_failed_child_is_an_honest_error(self):
        version = _target_version()
        stream = FakeStream(
            [_event(RunFailed, "child-1", error="boom", error_kind="model", total_usage=Usage())]
        )
        tooling = await _provider(FakeAgents({"worker": version}), stream=stream).resolve(
            [_BINDING], _parent_ctx()
        )
        with pytest.raises(SubAgentError, match="boom"):
            await tooling.tools[0].execute({"task": "go"}, _tool_context())

    async def test_failed_child_surfaces_as_error_result_through_the_runtime(self):
        version = _target_version()
        stream = FakeStream(
            [_event(RunFailed, "child-1", error="boom", error_kind="model", total_usage=Usage())]
        )
        tooling = await _provider(FakeAgents({"worker": version}), stream=stream).resolve(
            [_BINDING], _parent_ctx()
        )
        registry = InMemoryToolRegistry()
        registry.register(tooling.tools[0])
        runtime = ToolRuntime(registry)
        result = await runtime.execute(
            ToolCall(id="c1", name="sub__worker", arguments={"task": "go"}), _tool_context()
        )
        assert result.is_error is True
        assert "boom" in result.output

    async def test_child_cancelled_propagates_without_re_cancelling(self):
        version = _target_version()
        queue = FakeQueue()
        stream = FakeStream([_event(RunCancelled, "child-1", reason="user", total_usage=Usage())])
        tooling = await _provider(
            FakeAgents({"worker": version}), queue=queue, stream=stream
        ).resolve([_BINDING], _parent_ctx())
        with pytest.raises(ExecutionCancelled, match="user"):
            await tooling.tools[0].execute({"task": "go"}, _tool_context())
        assert queue.cancels == []  # the child is already cancelled

    async def test_parent_cancel_observed_cancels_child_and_reraises(self):
        version = _target_version()
        queue = FakeQueue()
        stream = FakeStream(
            [_event(RunStarted, "child-1", agent_id="worker", agent_version_id="v")]
        )
        ctx = _parent_ctx()
        ctx.cancel.trigger("cancelled by user")
        tooling = await _provider(
            FakeAgents({"worker": version}), queue=queue, stream=stream
        ).resolve([_BINDING], ctx)
        with pytest.raises(ExecutionCancelled, match="cancelled by user"):
            await tooling.tools[0].execute({"task": "go"}, _tool_context(cancel=ctx.cancel))
        assert queue.cancels == [(stream.subscribed[0], "parent stopped waiting")]

    async def test_parent_deadline_cancels_child_and_errors(self):
        """A past deadline → wait_for timeout 0 → the bounded ending without
        touching the child's stream (the sweeper never reaps pending rows,
        so this bound is what keeps the parent from blocking forever)."""
        version = _target_version()
        queue = FakeQueue()
        stream = FakeStream(hung=True)  # the child never emits
        ctx = _parent_ctx(deadline=datetime.now(UTC) - timedelta(seconds=1))
        tooling = await _provider(
            FakeAgents({"worker": version}), queue=queue, stream=stream
        ).resolve([_BINDING], ctx)
        with pytest.raises(SubAgentError, match="deadline expired"):
            await tooling.tools[0].execute({"task": "go"}, _tool_context(cancel=ctx.cancel))
        assert len(queue.cancels) == 1

    async def test_deadline_between_batches_is_an_error_not_a_reraise(self):
        """D52: a deadline observed by check_limits between batches is an
        error result (the parent's own next checkpoint turns it into the
        terminal), never a re-raise that would misreport run.cancelled."""
        version = _target_version()
        queue = FakeQueue()
        stream = FakeStream(
            [
                _event(RunStarted, "child-1", agent_id="worker", agent_version_id="v"),
                _event(
                    RunCompleted, "child-1", final_message="late", total_usage=Usage(), iterations=1
                ),
            ]
        )
        ctx = _parent_ctx(deadline=datetime.now(UTC) - timedelta(seconds=5))
        tooling = await _provider(
            FakeAgents({"worker": version}), queue=queue, stream=stream
        ).resolve([_BINDING], ctx)
        tool = tooling.tools[0]
        with pytest.raises(SubAgentError, match="aborted"):
            await tool._await_child("child-1", _tool_context(cancel=ctx.cancel))
        assert queue.cancels == [(stream.subscribed[0], "parent stopped waiting")]

    async def test_stream_ended_without_terminal_cancels_child_and_errors(self):
        version = _target_version()
        queue = FakeQueue()
        stream = FakeStream({})  # subscribes, yields nothing, ends
        tooling = await _provider(
            FakeAgents({"worker": version}), queue=queue, stream=stream
        ).resolve([_BINDING], _parent_ctx())
        with pytest.raises(SubAgentError, match="without a terminal"):
            await tooling.tools[0].execute({"task": "go"}, _tool_context())
        assert len(queue.cancels) == 1

    async def test_over_cap_delegation_refuses_without_enqueueing(self):
        version = _target_version()
        executions, queue = FakeExecutions(), FakeQueue()
        ctx = _parent_ctx(metadata={"sub_agent": {"depth": 2}})
        tooling = await _provider(
            FakeAgents({"worker": version}), executions, queue, max_depth=2
        ).resolve([_BINDING], ctx)
        with pytest.raises(SubAgentError, match="depth cap"):
            await tooling.tools[0].execute({"task": "go"}, _tool_context())
        assert executions.written == []  # refused before any child exists

    async def test_depth_zero_spawn_is_allowed_at_the_default_cap(self):
        version = _target_version()
        executions = FakeExecutions()
        stream = FakeStream(
            [_event(RunCompleted, "child-1", final_message="ok", total_usage=Usage(), iterations=1)]
        )
        tooling = await _provider(
            FakeAgents({"worker": version}), executions, stream=stream
        ).resolve([_BINDING], _parent_ctx())
        output = await tooling.tools[0].execute({"task": "go"}, _tool_context())
        assert output == "ok"
        assert executions.written[0][1].metadata == {
            "sub_agent": {"depth": 1},
            "auto_decline_approvals": True,
        }


def _tool_context(cancel=None):
    from jarvis.domain.tools import ToolContext

    return ToolContext(run_id="run-parent", agent_id="supervisor", cancel=cancel)


# --- the merged tooling (AgentRuntime._resolve_tooling) -------------------------


def _runtime_with(provider: SubAgentToolProvider | None, *, mcp=None):
    registry = InMemoryToolRegistry()
    from jarvis.tools.builtin.calculator import CalculatorTool

    registry.register(CalculatorTool())
    return AgentRuntime(
        strategies=DefaultStrategyRegistry(),
        tools=registry,
        tool_runtime=ToolRuntime(registry),
        models=object(),  # never resolved — _resolve_tooling only builds tooling
        mcp=mcp,
        sub_agents=provider,
    )


def _agent(**overrides) -> AgentDefinition:
    base = dict(
        id="supervisor",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        system_prompt="You are a test agent.",
    )
    base.update(overrides)
    return AgentDefinition(**base)


class TestMergedTooling:
    async def test_no_sub_bindings_keeps_the_default_tooling(self):
        runtime = _runtime_with(None)
        tooling = await runtime._resolve_tooling(_agent(), _parent_ctx())
        assert tooling.registry is runtime._tools
        assert tooling.tool_runtime is runtime._tool_runtime

    async def test_sub_binding_without_a_provider_is_a_resolution_error(self):
        runtime = _runtime_with(None)
        with pytest.raises(SubAgentResolutionError) as excinfo:
            await runtime._resolve_tooling(
                _agent(tools=[ToolBinding(name="sub__worker")]), _parent_ctx()
            )
        assert "sub__worker" in str(excinfo.value)
        # ...and it rides the shared base the catch sites already catch
        assert isinstance(excinfo.value, ToolResolutionError)

    async def test_merged_registry_carries_builtins_and_sub_tools(self):
        version = _target_version()
        provider = _provider(FakeAgents({"worker": version}))
        runtime = _runtime_with(provider)
        tooling = await runtime._resolve_tooling(
            _agent(tools=[ToolBinding(name="calculator"), _BINDING]), _parent_ctx()
        )
        assert "calculator" in tooling.registry
        assert "sub__worker" in tooling.registry
        assert tooling.tool_runtime is not runtime._tool_runtime
        # a fresh ToolRuntime wraps the merged view, and aclose composes
        await tooling.aclose()

    async def test_settings_default_depth_is_two(self):
        assert Settings(_env_file=None).sub_agent_max_depth == 2
