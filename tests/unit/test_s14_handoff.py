"""S14 commit 7 — handoffs (D56): the transfer tool (transcript render +
truncation, successor enqueue shape), the runtime's post-batch handoff →
ordinary `run.completed` mapping, and mixed batches (other calls persist
first). The provider's family resolution is covered where it belongs
(test_subagent_provider)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import is_terminal
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockModelProvider, turn
from jarvis.runtime.agent_runtime import AgentRuntime, LoopOutcome
from jarvis.runtime.child_runs import child_message, enqueue_child_run
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.base import BaseTool
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from jarvis.tools.subagents.handoff import HandoffTool, mint_handoff_descriptor, render_transcript
from tests.unit.test_agent_runtime import _RecordingRepo
from tests.unit.test_subagent_provider import FakeExecutions, _target_version

# --- the doubles ---------------------------------------------------------------


class _FakeTranscripts:
    """Serves a scripted transcript from list_messages (the parent's run)."""

    def __init__(self, messages: list[Message]):
        self._messages = messages

    async def list_messages(self, run_id: str) -> list[Message]:
        return list(self._messages)


_TRANSCRIPT = [
    Message(role="user", content="find the root cause of the flaky test"),
    Message(role="assistant", content="I narrowed it to the race in the sweeper"),
    Message(role="tool", content="4", tool_call_id="c1", name="calculator"),
]


def _parent(**kw) -> ExecutionContext:
    base: dict[str, Any] = dict(
        run_id="run-parent",
        agent_id="agent-a",
        agent_version_id="ver-a",
        tenant_id="t1",
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        session_id="sess-1",
        user_id="u1",
    )
    base.update(kw)
    return ExecutionContext(**base)


def _tool_context() -> Any:
    from jarvis.domain.tools import ToolContext

    return ToolContext(run_id="run-parent", agent_id="agent-a")


def _handoff_tool(parent: ExecutionContext, executions: FakeExecutions, *, depth=0) -> HandoffTool:
    version = _target_version()
    return HandoffTool(
        mint_handoff_descriptor("handoff__worker", version),
        agent_id="worker",
        agent_name=version.snapshot.name,
        version_id=version.id,
        parent_ctx=parent,
        executions=executions,
        transcripts=_FakeTranscripts(list(_TRANSCRIPT)),
        max_transcript_chars=20_000,
        parent_depth=depth,
    )


# --- the transcript render -------------------------------------------------------


class TestRenderTranscript:
    def test_roles_render_as_labeled_text(self):
        text = render_transcript(list(_TRANSCRIPT), max_chars=10_000)
        assert text.splitlines() == [
            "[user] find the root cause of the flaky test",
            "[assistant] I narrowed it to the race in the sweeper",
            "[tool calculator] 4",
        ]

    def test_truncation_drops_the_oldest_first(self):
        messages = [Message(role="user", content=f"old message {i}") for i in range(50)]
        messages.append(Message(role="user", content="the newest and most important message"))
        text = render_transcript(messages, max_chars=200)
        assert "[earlier transcript truncated]" in text
        assert text.index("earlier transcript truncated") < text.index("newest")
        assert "old message 0" not in text  # the oldest dropped first
        assert "the newest and most important message" in text  # the tail kept

    def test_a_single_oversized_message_is_kept(self):
        # truncation never empties the transcript — one long message stays
        text = render_transcript([Message(role="user", content="x" * 500)], max_chars=10)
        assert "x" in text

    def test_empty_transcript_renders_empty(self):
        assert render_transcript([], max_chars=200) == ""


# --- the tool: successor message shape -------------------------------------------


class TestHandoffTool:
    async def test_successor_message_shape(self):
        parent = _parent()
        executions = FakeExecutions()
        version = _target_version()
        tool = HandoffTool(
            mint_handoff_descriptor("handoff__worker", version),
            agent_id="worker",
            agent_name=version.snapshot.name,
            version_id=version.id,
            parent_ctx=parent,
            executions=executions,
            transcripts=_FakeTranscripts(list(_TRANSCRIPT)),
            max_transcript_chars=20_000,
            parent_depth=1,
        )
        output = await tool.execute({"note": "please take over"}, _tool_context())
        assert output.startswith("Handed off to Worker (run ")
        result, message = executions.written[0]
        assert result.status == "queued"
        assert message.agent_id == "worker"
        assert message.agent_version_id == version.id
        assert message.parent_run_id == "run-parent"
        assert message.trace_id == "trace-1"
        assert message.session_id == "sess-1"  # the PARENT's session (D56)
        assert message.tenant_id == "t1"
        assert message.deadline == parent.deadline
        assert message.metadata == {
            "handoff": {"note": "please take over"},
            "sub_agent": {"depth": 1},  # the depth rides forward, unchanged
        }
        # the successor's input is the rendered transcript + the note
        assert "[user] find the root cause" in message.input
        assert "[assistant] I narrowed it" in message.input
        assert message.input.endswith("[handoff note] please take over")

    async def test_successor_copies_parent_trigger_provenance_verbatim(self):
        """S13 (ADR 0019 §6): the handoff copy mirrors the sub-agent child's
        — a handoff chain cannot launder trigger provenance any more than
        it can launder the depth cap. A parent without it composes
        byte-identical successors (the shape test above gates that)."""
        trigger_meta = {"trigger_id": "trg-1", "kind": "event", "name": "watcher", "depth": 1}
        executions = FakeExecutions()
        version = _target_version()
        tool = HandoffTool(
            mint_handoff_descriptor("handoff__worker", version),
            agent_id="worker",
            agent_name=version.snapshot.name,
            version_id=version.id,
            parent_ctx=_parent(metadata={"trigger": trigger_meta}),
            executions=executions,
            transcripts=_FakeTranscripts(list(_TRANSCRIPT)),
            max_transcript_chars=20_000,
            parent_depth=1,
        )
        await tool.execute({"note": None}, _tool_context())
        _, message = executions.written[0]
        assert message.metadata["trigger"] == trigger_meta
        assert message.metadata["handoff"] == {"note": None}
        assert message.metadata["sub_agent"] == {"depth": 1}

    async def test_no_note_keeps_a_plain_transcript_input(self):
        parent = _parent()
        executions = FakeExecutions()
        tool = HandoffTool(
            mint_handoff_descriptor("handoff__worker", _target_version()),
            agent_id="worker",
            agent_name="Worker",
            version_id="ver-w",
            parent_ctx=parent,
            executions=executions,
            transcripts=_FakeTranscripts(list(_TRANSCRIPT)),
            max_transcript_chars=20_000,
            parent_depth=0,
        )
        await tool.execute({}, _tool_context())
        _, message = executions.written[0]
        assert message.metadata["handoff"] == {"note": None}
        assert "[handoff note]" not in message.input

    async def test_reports_the_handoff_through_the_context_channel(self):
        tool = HandoffTool(
            mint_handoff_descriptor("handoff__worker", _target_version()),
            agent_id="worker",
            agent_name="Worker",
            version_id="ver-w",
            parent_ctx=_parent(),
            executions=FakeExecutions(),
            transcripts=_FakeTranscripts(list(_TRANSCRIPT)),
            max_transcript_chars=20_000,
            parent_depth=0,
        )
        ctx = _tool_context()
        output = await tool.execute({"note": "go"}, ctx)
        successor_run_id = output.rsplit("(run ", 1)[1].rstrip(")")
        assert ctx.result_metadata == {
            "kind": "handoff",
            "successor_run_id": successor_run_id,
            "agent_id": "worker",
            "agent_name": "Worker",
        }

    async def test_descriptor_declares_an_optional_note_never_gated(self):
        descriptor = mint_handoff_descriptor("handoff__worker", _target_version())
        assert descriptor.parameters["required"] == []
        assert "note" in descriptor.parameters["properties"]
        assert descriptor.annotations == {"requires_approval": False}

    async def test_the_enqueue_composition_is_shared_with_the_sub_family(self):
        """The successor is an ordinary queue run through the SAME helper the
        sub__ family uses — one message shape, one enqueue path."""
        message = child_message(
            agent_id="worker",
            agent_version_id="ver-w",
            input="t",
            tenant_id="t1",
            trace_id="trace-1",
            deadline=None,
            parent_run_id="run-parent",
            metadata={"handoff": {"note": "go"}},
        )
        executions = FakeExecutions()
        run_id = await enqueue_child_run(executions=executions, message=message)
        assert run_id == message.run_id
        result, written = executions.written[0]
        assert result.status == "queued"
        assert result.metadata == {"handoff": {"note": "go"}}
        assert written.parent_run_id == "run-parent"


# --- the runtime outcome mapping --------------------------------------------------


class _ScriptedHandoffTool(BaseTool):
    """Mimics the real HandoffTool's runtime contract (enqueue + report
    through the channel) without the transcript machinery."""

    def __init__(self, executions: FakeExecutions, agent_name: str = "Worker"):
        super().__init__(
            ToolDescriptor(
                # a NEUTRAL name: the runtime detects the handoff outcome from
                # result metadata (`kind: "handoff"`), never the name — a
                # real `handoff__*` binding would demand the sub-agent
                # provider this scripted stand-in deliberately omits
                name="relay__worker",
                description="hand off",
                parameters={"type": "object", "properties": {}},
                annotations={"requires_approval": False},
            )
        )
        self._executions = executions
        self._agent_name = agent_name

    async def _execute(self, arguments: dict[str, Any], context: Any) -> str:
        message = child_message(
            agent_id="worker",
            agent_version_id="ver-w",
            input="transferred",
            tenant_id="t1",
            trace_id="trace-1",
            deadline=None,
            parent_run_id="run-h",
            metadata={"handoff": {"note": None}},
        )
        run_id = await enqueue_child_run(executions=self._executions, message=message)
        context.result_metadata.update(
            {
                "kind": "handoff",
                "successor_run_id": run_id,
                "agent_id": "worker",
                "agent_name": self._agent_name,
            }
        )
        return f"Handed off to {self._agent_name} (run {run_id})"


def _agent(**overrides) -> AgentDefinition:
    base = dict(
        id="a1",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        system_prompt="You are a test agent.",
    )
    base.update(overrides)
    return AgentDefinition(**base)


def _version(agent: AgentDefinition) -> AgentVersion:
    return AgentVersion(id=str(uuid4()), agent_id=agent.id, version=1, snapshot=agent)


def _ctx(run_id: str, **kw) -> ExecutionContext:
    return ExecutionContext(run_id=run_id, agent_id="a1", agent_version_id="v1", **kw)


def _runtime(provider: MockModelProvider, tools, repo) -> AgentRuntime:
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        strategies=DefaultStrategyRegistry(),
        tools=registry,
        tool_runtime=ToolRuntime(registry),
        models=DefaultModelProviderFactory(mock_provider=provider),
        conversations=repo,
        executions=repo,
    )


class TestRuntimeHandoffOutcome:
    async def test_handoff_terminates_the_predecessor_as_run_completed(self):
        repo = _RecordingRepo()
        executions = FakeExecutions()
        provider = MockModelProvider(
            [
                turn(tool_calls=[ToolCall(id="c1", name="relay__worker", arguments={})]),
                turn("never reached"),
            ]
        )
        runtime = _runtime(provider, [_ScriptedHandoffTool(executions)], repo)
        result = await runtime.run(
            _version(_agent(tools=[ToolBinding(name="relay__worker")])),
            "go",
            _ctx("run-h"),
        )

        assert result.status == "succeeded"  # no new terminal status (D56)
        successor_run_id = executions.written[0][1].run_id
        assert result.final_message == f"Handed off to Worker (run {successor_run_id})"
        events = runtime.bus.get("run-h").events
        assert [e.type for e in events if is_terminal(e)] == ["run.completed"]
        # the run ENDS after the batch — the second model turn never runs
        assert provider.invocations == 1

    async def test_mixed_batch_other_calls_persist_first(self):
        repo = _RecordingRepo()
        executions = FakeExecutions()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "2+2"}),
                        ToolCall(id="c2", name="relay__worker", arguments={}),
                    ]
                ),
                turn("never reached"),
            ]
        )
        runtime = _runtime(provider, [CalculatorTool(), _ScriptedHandoffTool(executions)], repo)
        result = await runtime.run(
            _version(
                _agent(tools=[ToolBinding(name="calculator"), ToolBinding(name="relay__worker")])
            ),
            "go",
            _ctx("run-h2"),
        )

        assert result.status == "succeeded"
        # both calls executed and persisted; the handoff ended the run after
        tool_messages = [m for m in repo.messages["run-h2"] if m.role == "tool"]
        assert len(tool_messages) == 2
        assert tool_messages[0].content == "4"
        assert tool_messages[1].content.startswith("Handed off to Worker")
        successor_run_id = executions.written[0][1].run_id
        assert result.final_message == f"Handed off to Worker (run {successor_run_id})"

    async def test_the_handoff_outcome_defaults_keep_other_runs_identical(self):
        """A regression guard on the S10 resumed-batch branch: the loop
        variable is initialized for BOTH batch entries, and a plain outcome
        carries no handoff."""
        outcome = LoopOutcome(kind="completed", final_message="x")
        assert outcome.handoff is None
