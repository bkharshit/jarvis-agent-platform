"""MCP resolution through the REAL AgentRuntime loop (S4 commit 4, D38) —
no loop, sink, or ToolRuntime mocks; only the MCP provider's repo and
connections are faked. Covers the four plan behaviors: resolution failure is
a persisted terminal `tool` failure (so the worker acks, never retries),
recoverable call failure mid-run, the approval-default pause → decisions →
resume path with per-segment re-resolution, and byte-identical behavior with
no `mcp__*` bindings."""

from __future__ import annotations

from typing import Any

from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.events import is_terminal, validate_event_sequence
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.mcp import McpHttpConfig, McpServer, McpStdioConfig
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.events.bus import InProcessEventSink
from jarvis.models.mock import MockModelProvider, turn
from jarvis.ports.queue import ResumeRequest
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.mcp.provider import McpToolProvider
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from tests.unit.test_agent_runtime import _RecordingRepo  # the shared doubles

# --- the fake provider seam ---------------------------------------------------


class FakeRepo:
    def __init__(self, names: set[str]):
        self._names = names
        self.lookups: list[tuple[str | None, str]] = []

    async def get_by_name(self, name: str, *, tenant_id: str | None = None):
        self.lookups.append((tenant_id, name))
        if name not in self._names:
            return None
        return McpServer(
            id=f"srv-{name}", name=name, config=McpStdioConfig(type="stdio", command="x")
        )


class FakeConnection:
    def __init__(self, name: str, tools: list[str], *, call_error: Exception | None = None):
        self.server_name = name
        self._tools = tools
        self._call_error = call_error
        self.closed = False
        self.calls: list[str] = []

    @property
    def raw_names(self) -> dict[str, str]:
        return {f"mcp__{self.server_name}__{t}": t for t in self._tools}

    async def connect(self) -> None: ...

    async def close(self) -> None:
        self.closed = True

    async def descriptors(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name=name,
                description="fake",
                parameters={"type": "object", "properties": {}},
                annotations={"requires_approval": True, "timeout": None},
            )
            for name in self.raw_names
        ]

    async def call(self, raw_tool: str, arguments: dict[str, Any]) -> str:
        self.calls.append(raw_tool)
        if self._call_error is not None:
            raise self._call_error
        return f"{raw_tool} ok"


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
    from uuid import uuid4

    return AgentVersion(id=str(uuid4()), agent_id=agent.id, version=1, snapshot=agent)


def _ctx(run_id: str) -> ExecutionContext:
    return ExecutionContext(run_id=run_id, agent_id="a1", agent_version_id="v1", tenant_id="t1")


def _runtime(
    provider: MockModelProvider,
    *,
    rows: set[str] | None = None,
    connections: dict[str, FakeConnection] | None = None,
    repo: _RecordingRepo | None = None,
    mcp: McpToolProvider | None = None,
) -> AgentRuntime:
    base = InMemoryToolRegistry()
    if mcp is None:
        mcp = McpToolProvider(
            FakeRepo(rows or set()),
            base,
            Settings(_env_file=None),
            connection_factory=lambda server: connections[server.name],  # type: ignore[index]
        )
    return AgentRuntime(
        strategies=DefaultStrategyRegistry(),
        tools=base,
        tool_runtime=ToolRuntime(base),
        models=_factory(provider),
        mcp=mcp,
        conversations=repo,
        executions=repo,
    )


def _factory(provider: MockModelProvider):
    from jarvis.models.factory import DefaultModelProviderFactory

    return DefaultModelProviderFactory(mock_provider=provider)


# --- terminal resolution failure -----------------------------------------------


class TestResolutionFailure:
    async def test_unknown_server_is_terminal_tool_failure_and_never_raises(self):
        provider = MockModelProvider([turn("never reached — no tokens spent")])
        repo = _RecordingRepo()
        runtime = _runtime(provider, rows=set(), repo=repo)

        result = await runtime.run(
            _version(_agent(tools=[ToolBinding(name="mcp__ghost__echo")])),
            "hi",
            _ctx("run-mcp-fail-1"),
        )

        # run() returns normally — the worker acks instead of retrying forever
        assert result.status == "failed"
        assert result.error_kind == "tool"
        assert "ghost" in (result.error or "")
        # exactly one persisted terminal, before any model token was spent
        assert [r.status for r in repo.finished] == ["failed"]
        assert repo.runs["run-mcp-fail-1"].status == "failed"
        assert provider.invocations == 0
        events = runtime.bus.get("run-mcp-fail-1").events
        terminal = [e for e in events if is_terminal(e)]
        assert len(terminal) == 1
        assert terminal[0].type == "run.failed"
        assert terminal[0].error_kind == "tool"

    async def test_resume_resolution_failure_is_terminal_tool_failure(self):
        provider = MockModelProvider([turn("never")])
        repo = _RecordingRepo()
        runtime = _runtime(provider, rows=set(), repo=repo)
        agent = _agent(tools=[ToolBinding(name="mcp__ghost__echo")])
        result = await runtime.resume(
            _version(agent),
            "run-mcp-fail-2",
            _ctx("run-mcp-fail-2"),
            InProcessEventSink("run-mcp-fail-2", sequence_offset=4),
            ResumeRequest(kind="content", content="go"),
        )
        assert result.status == "failed"
        assert result.error_kind == "tool"
        assert "ghost" in (result.error or "")
        assert [r.status for r in repo.finished] == ["failed"]

    async def test_stored_header_ref_without_a_resolver_is_terminal_tool_failure(self):
        """ADR 0013: a stored credential ref on a REAL connection with no
        resolver wired fails resolution — the honest terminal tool failure
        (D38), never an exception past the runtime."""
        provider = MockModelProvider([turn("never")])
        repo = _RecordingRepo()
        base = InMemoryToolRegistry()

        class _HttpRepo:
            async def get_by_name(self, name, *, tenant_id=None):
                if name != "httpx":
                    return None
                return McpServer(
                    id="srv-http",
                    name="httpx",
                    config=McpHttpConfig(
                        type="http",
                        url="https://x/mcp",
                        headers={"Authorization": {"type": "stored", "credential_id": "cred-9"}},
                    ),
                )

        mcp = McpToolProvider(_HttpRepo(), base, Settings(_env_file=None))  # no resolver
        runtime = _runtime(provider, repo=repo, mcp=mcp)
        result = await runtime.run(
            _version(_agent(tools=[ToolBinding(name="mcp__httpx__echo")])),
            "hi",
            _ctx("run-mcp-fail-3"),
        )
        assert result.status == "failed"
        assert result.error_kind == "tool"
        assert "cred-9" in (result.error or "")
        assert provider.invocations == 0
        terminal = [e for e in runtime.bus.get("run-mcp-fail-3").events if is_terminal(e)]
        assert [t.type for t in terminal] == ["run.failed"]


# --- recoverable call failure ---------------------------------------------------


class TestRecoverableCallFailure:
    async def test_call_failure_is_error_tool_result_and_run_completes(self):
        connection = FakeConnection("fixtures", ["echo"], call_error=ValueError("server blew up"))
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="mcp__fixtures__echo", arguments={"text": "hi"})
                    ]
                ),
                turn("handled the failure"),
            ]
        )
        repo = _RecordingRepo()
        runtime = _runtime(
            provider, rows={"fixtures"}, connections={"fixtures": connection}, repo=repo
        )

        # binding config ungates the MCP approval default (S10 binding-wins
        # rule unchanged) so the failing call actually executes
        result = await runtime.run(
            _version(
                _agent(
                    tools=[
                        ToolBinding(name="mcp__fixtures__echo", config={"requires_approval": False})
                    ]
                )
            ),
            "hi",
            _ctx("run-mcp-call-1"),
        )

        # D38: execution is not the boundary — the failure is a recoverable
        # error ToolResult and the loop continues to a normal terminal.
        assert result.status == "succeeded"
        assert result.final_message == "handled the failure"
        events = runtime.bus.get("run-mcp-call-1").events
        types = [e.type for e in events]
        assert "tool.call.failed" in types
        assert types[-1] == "run.completed"
        assert len([e for e in events if is_terminal(e)]) == 1
        failed = [e for e in events if e.type == "tool.call.failed"][0]
        assert "server blew up" in failed.error
        # the error reached the transcript as the tool message
        tool_messages = [m for m in repo.messages["run-mcp-call-1"] if m.role == "tool"]
        assert tool_messages and "server blew up" in tool_messages[-1].content
        # the segment's connection closed when the run ended
        assert connection.closed


# --- approval default: pause → decisions → resume -------------------------------


class TestMcpApprovalPauseResume:
    def _seed(self, repo: _RecordingRepo, sink: InProcessEventSink) -> None:
        for event in sink.events:
            repo.record_event(event)

    async def test_default_approval_pauses_then_decisions_execute_and_resume_reresolves(self):
        connections: list[FakeConnection] = []

        def factory(server):
            conn = FakeConnection("fixtures", ["echo"])
            connections.append(conn)
            return conn

        base = InMemoryToolRegistry()
        mcp = McpToolProvider(
            FakeRepo({"fixtures"}),
            base,
            Settings(_env_file=None),
            connection_factory=factory,
        )
        model = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="mcp__fixtures__echo", arguments={"text": "hi"})
                    ]
                ),
                turn("echo says hi"),
            ]
        )
        repo = _RecordingRepo()
        runtime = AgentRuntime(
            strategies=DefaultStrategyRegistry(),
            tools=base,
            tool_runtime=ToolRuntime(base),
            models=_factory(model),
            mcp=mcp,
            conversations=repo,
            executions=repo,
        )
        agent = _agent(tools=[ToolBinding(name="mcp__fixtures__echo")])

        first = await runtime.run(_version(agent), "hi", _ctx("run-mcp-approve-1"))
        assert first.status == "awaiting_input"
        assert repo.finished == []
        first_events = runtime.bus.get("run-mcp-approve-1").events
        first_types = [e.type for e in first_events]
        assert "tool.call.requested" in first_types
        assert "tool.call.started" not in first_types  # nothing executed pre-approval
        assert first_events[-1].pending_calls[0].name == "mcp__fixtures__echo"
        # the first segment's connection closed with the pause
        assert connections[0].closed
        self._seed(repo, runtime.bus.get("run-mcp-approve-1"))

        sink = InProcessEventSink("run-mcp-approve-1", sequence_offset=len(first_events))
        done = await runtime.resume(
            _version(agent),
            "run-mcp-approve-1",
            _ctx("run-mcp-approve-1"),
            sink,
            ResumeRequest(kind="decisions", decisions={"c1": True}),
        )

        # the approved call executed and the run completed
        assert done.status == "succeeded"
        assert done.final_message == "echo says hi"
        sink_types = [e.type for e in sink.events]
        assert sink_types[:3] == ["tool.call.started", "tool.call.completed", "iteration.completed"]
        assert sink_types[-1] == "run.completed"
        assert [r.status for r in repo.finished] == ["succeeded"]
        # resume RE-RESOLVED (D38): a second connection was opened and closed
        assert len(connections) == 2
        assert connections[1].closed
        assert connections[1].calls == ["echo"]
        # gapless across the segment boundary
        assert [e.sequence for e in sink.events] == list(
            range(len(first_events), len(first_events) + len(sink.events))
        )
        validate_event_sequence(first_events + sink.events)


# --- no-mcp bindings: byte-identical ---------------------------------------------


class TestNoMcpUnchanged:
    async def test_builtin_only_agent_with_provider_wired_matches_provider_free(self):
        from jarvis.tools.builtin.calculator import CalculatorTool

        def _never(server):  # any resolution attempt would fail the test
            raise AssertionError("no mcp__ binding — no connection may be built")

        def _build(run_id: str):
            repo = _RecordingRepo()
            base = InMemoryToolRegistry()
            base.register(CalculatorTool())
            kw: dict[str, Any] = dict(
                strategies=DefaultStrategyRegistry(),
                tools=base,
                tool_runtime=ToolRuntime(base),
                models=_factory(
                    MockModelProvider(
                        [
                            turn(
                                tool_calls=[
                                    ToolCall(
                                        id="c1", name="calculator", arguments={"expression": "6*7"}
                                    )
                                ]
                            ),
                            turn("42 it is"),
                        ]
                    )
                ),
                conversations=repo,
                executions=repo,
            )
            return repo, kw

        plain_repo, plain_kw = _build("run-mcp-plain")
        plain = AgentRuntime(**plain_kw)
        plain_result = await plain.run(
            _version(_agent(tools=[ToolBinding(name="calculator")])), "x", _ctx("run-mcp-plain")
        )

        wired_repo, wired_kw = _build("run-mcp-wired")
        wired_base = wired_kw["tools"]
        wired_kw["mcp"] = McpToolProvider(
            FakeRepo(set()), wired_base, Settings(_env_file=None), connection_factory=_never
        )
        wired = AgentRuntime(**wired_kw)
        wired_result = await wired.run(
            _version(_agent(tools=[ToolBinding(name="calculator")])), "x", _ctx("run-mcp-wired")
        )

        assert plain_result.status == wired_result.status == "succeeded"
        assert wired_result.final_message == plain_result.final_message == "42 it is"
        wired_types = [e.type for e in wired.bus.get("run-mcp-wired").events]
        plain_types = [e.type for e in plain.bus.get("run-mcp-plain").events]
        assert wired_types == plain_types
        assert wired_types[-1] == "run.completed"
