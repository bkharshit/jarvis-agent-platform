"""S9 (D78): parallel tool-call batches — the per-agent opt-in behind the
provider capability. Default stays byte-sequential; ordering (messages,
events) stays call-ordered on both paths; a failing sibling never cancels
the others mid-flight (D6)."""

import asyncio

from jarvis.domain.events import validate_event_sequence
from jarvis.domain.execution import ExecutionCancelled
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.mock import MockModelProvider, turn
from jarvis.tools.base import BaseTool
from tests.unit.test_agent_runtime import (
    _agent,
    _ctx,
    _RecordingRepo,
    _runtime,
    _version,
)


class _TimedTool(BaseTool):
    """Sleeps then records its start/end monotonic marks — the concurrency
    proof reads the windows: a parallel batch's windows overlap, a
    sequential batch's don't."""

    def __init__(self, name: str, delay: float, marks: list[tuple[str, float, float]]):
        super().__init__(ToolDescriptor(name=name, description="slow", parameters={}))
        self._delay = delay
        self._marks = marks

    async def _execute(self, arguments, context):
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(self._delay)
        end = asyncio.get_running_loop().time()
        self._marks.append((self.descriptor.name, start, end))
        return f"done:{self.descriptor.name}"


def _multi_call_turn() -> list:
    return [
        turn(
            tool_calls=[
                ToolCall(id="c1", name="slow_a", arguments={}),
                ToolCall(id="c2", name="slow_b", arguments={}),
            ]
        ),
        turn("both done"),
    ]


def _parallel_provider() -> MockModelProvider:
    # Fresh per test — MockTurn lists are consumed FIFO; a shared provider
    # would leak scripted turns across tests.
    return MockModelProvider(
        _multi_call_turn(), capabilities=ModelCapabilities(parallel_tool_calls=True)
    )


def _sequential_provider() -> MockModelProvider:
    return MockModelProvider(_multi_call_turn())


def _overlapped(marks: list[tuple[str, float, float]]) -> bool:
    (a_start, a_end), (b_start, b_end) = marks[0][1:], marks[1][1:]
    return a_start < b_end and b_start < a_end


class TestParallelToolBatches:
    async def test_default_stays_sequential(self):
        marks: list[tuple[str, float, float]] = []
        runtime = _runtime(
            _sequential_provider(),
            tools=[_TimedTool("slow_a", 0.05, marks), _TimedTool("slow_b", 0.05, marks)],
        )

        result = await runtime.run(_version(_agent()), "compute", _ctx("run-seq-1"))

        assert result.status == "succeeded"
        # Non-overlapping windows — one finished before the other started.
        a_start, a_end = marks[0][1:]
        b_start, b_end = marks[1][1:]
        assert a_end <= b_start or b_end <= a_start
        # Started/completed frames strictly interleave per call.
        types = [e.type for e in runtime.bus.get("run-seq-1").events]
        starts = [i for i, t in enumerate(types) if t == "tool.call.started"]
        dones = [i for i, t in enumerate(types) if t in ("tool.call.completed", "tool.call.failed")]
        assert starts[0] < dones[0] < starts[1] < dones[1]
        validate_event_sequence(runtime.bus.get("run-seq-1").events)

    async def test_opted_in_batch_runs_concurrently_in_call_order(self):
        marks: list[tuple[str, float, float]] = []
        repo = _RecordingRepo()
        agent = _agent(parallel_tool_calls=True)
        runtime = _runtime(
            _parallel_provider(),
            tools=[_TimedTool("slow_a", 0.05, marks), _TimedTool("slow_b", 0.05, marks)],
            repo=repo,
        )

        result = await runtime.run(_version(agent), "compute", _ctx("run-par-1"))

        assert result.status == "succeeded"
        assert result.final_message == "both done"
        a_start, a_end = marks[0][1:]
        b_start, b_end = marks[1][1:]
        # Overlapping windows — the awaits ran concurrently.
        assert a_start < b_end and b_start < a_end
        # Events stay deterministic: started frames for the whole batch
        # before either completed frame; completed frames in call order.
        events = runtime.bus.get("run-par-1").events
        started_names = [e.name for e in events if e.type == "tool.call.started"]
        assert started_names == ["slow_a", "slow_b"]
        completed_names = [e.name for e in events if e.type == "tool.call.completed"]
        assert completed_names == ["slow_a", "slow_b"]
        # Tool messages persist in call order.
        tool_msgs = [m for m in repo.messages["run-par-1"] if m.role == "tool"]
        assert [m.content for m in tool_msgs] == ["done:slow_a", "done:slow_b"]
        validate_event_sequence(events)

    async def test_agent_opt_in_without_provider_capability_stays_sequential(self):
        marks: list[tuple[str, float, float]] = []
        agent = _agent(parallel_tool_calls=True)  # capability default False
        runtime = _runtime(
            _sequential_provider(),
            tools=[_TimedTool("slow_a", 0.05, marks), _TimedTool("slow_b", 0.05, marks)],
        )

        result = await runtime.run(_version(agent), "compute", _ctx("run-gate-1"))

        assert result.status == "succeeded"
        a_start, a_end = marks[0][1:]
        b_start, b_end = marks[1][1:]
        assert a_end <= b_start or b_end <= a_start

    async def test_failing_sibling_does_not_cancel_the_others(self):
        class _BoomTool(BaseTool):
            def __init__(self):
                super().__init__(ToolDescriptor(name="boom", description="explodes", parameters={}))

            async def _execute(self, arguments, context):
                # ExecutionCancelled is the one exception ToolRuntime
                # re-raises — the escape that proves the gather holds
                # siblings open (no mid-flight asyncio-cancel, D6).
                await asyncio.sleep(0.02)
                raise ExecutionCancelled("boom")

        marks: list[tuple[str, float, float]] = []
        agent = _agent(parallel_tool_calls=True)
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="slow_a", arguments={}),
                        ToolCall(id="c2", name="boom", arguments={}),
                    ]
                ),
                turn("both done"),
            ],
            capabilities=ModelCapabilities(parallel_tool_calls=True),
        )
        runtime = _runtime(provider, tools=[_TimedTool("slow_a", 0.05, marks), _BoomTool()])

        result = await runtime.run(_version(agent), "compute", _ctx("run-err-1"))

        # The failure is terminal (cancelled), but the sibling ran to
        # completion: its mark landed and its completed frame was emitted
        # before the raise replayed the failure in call order.
        assert result.status == "cancelled"
        assert len(marks) == 1 and marks[0][0] == "slow_a"
        types = [e.type for e in runtime.bus.get("run-err-1").events]
        assert types.count("tool.call.completed") == 1
        assert types.count("tool.call.failed") == 0
