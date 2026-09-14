"""ConversationMemory unit tests (S12, ADR 0016 §2, D45).

The compaction semantics are exercised directly against in-memory doubles:
the summarize strategy's one scripted `generate()`, the rolling boundary,
degrade-on-ModelError, cancellation propagation, and the read-only
resume view. Runtime-level compaction (turn ordering inside a segment)
lives in test_agent_runtime.TestSummarizeStrategy.
"""

from __future__ import annotations

from jarvis.domain.agent import AgentDefinition, MemoryConfig, ModelRef, StrategyConfig
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, Usage
from jarvis.models.client import BoundModelClient
from jarvis.models.errors import ModelAbortedError, ModelError
from jarvis.models.mock import MockModelProvider, turn
from jarvis.runtime.memory import ConversationMemory, MemoryView


class _Repo:
    """In-memory ConversationRepo subset: history + S12 summary state."""

    def __init__(self) -> None:
        self.conversations: dict[str, list[Message]] = {}
        self.summary_state: dict[str, tuple[str, int]] = {}

    def seed(self, conversation_id: str, messages: list[Message]) -> None:
        self.conversations[conversation_id] = list(messages)

    # ConversationRepo
    async def get_or_create(self, agent_id, session_id, *, tenant_id=None):
        return f"{agent_id}:{session_id}"

    async def history(self, conversation_id, limit=None):
        messages = self.conversations.get(conversation_id, [])
        return messages[-limit:] if limit is not None else list(messages)

    async def get_summary_state(self, conversation_id, *, tenant_id=None):
        state = self.summary_state.get(conversation_id)
        if state is None:
            return None
        from jarvis.domain.agent import ConversationMemoryState

        return ConversationMemoryState(summary=state[0], summarized_count=state[1])

    async def save_summary(self, conversation_id, *, summary, summarized_count, tenant_id=None):
        self.summary_state[conversation_id] = (summary, summarized_count)


def _agent(
    *, enabled: bool = True, strategy: str = "summarize", window: int = 2
) -> AgentDefinition:
    return AgentDefinition(
        id="a1",
        name="memory-agent",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        memory=MemoryConfig(enabled=enabled, max_messages=window, strategy=strategy),
    )


def _ctx(**kw) -> ExecutionContext:
    base = dict(run_id="r1", agent_id="a1", agent_version_id="v1", session_id="s1")
    base.update(kw)
    return ExecutionContext(**base)


def _client(provider: MockModelProvider) -> BoundModelClient:
    return BoundModelClient(provider, ModelRef(provider="mock", model="mock-1"))


def _prior_history(n: int) -> list[Message]:
    return [
        Message(role="user" if i % 2 == 0 else "assistant", content=f"message {i}")
        for i in range(n)
    ]


class TestLoad:
    async def test_window_strategy_is_pure_read(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(6))
        provider = MockModelProvider()
        ctx = _ctx()

        view = await ConversationMemory(repo).load(
            _agent(strategy="window"), ctx, client=_client(provider)
        )

        assert provider.invocations == 0  # window never calls the model
        assert view.conversation_id == "a1:s1"
        assert len(view.history) == 6
        assert view.summary is None
        assert repo.summary_state == {}

    async def test_within_window_never_compacts(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(2))
        provider = MockModelProvider()
        ctx = _ctx()

        view = await ConversationMemory(repo).load(_agent(), ctx, client=_client(provider))

        assert provider.invocations == 0
        assert view.summary is None and repo.summary_state == {}

    async def test_memory_disabled_is_empty_view(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(6))
        view = await ConversationMemory(repo).load(
            _agent(enabled=False), _ctx(), client=_client(MockModelProvider())
        )
        assert view == MemoryView(history=[])
        assert repo.conversations["a1:s1"]  # untouched

    async def test_no_session_is_empty_view(self):
        repo = _Repo()
        view = await ConversationMemory(repo).load(_agent(), _ctx(session_id=None), client=None)
        assert view == MemoryView(history=[])


class TestCompaction:
    async def test_compaction_consumes_turn_and_saves_state(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(6))  # window 2 → 4 evictable
        provider = MockModelProvider(
            [turn("compressed history", usage=Usage(input_tokens=11, output_tokens=7))]
        )
        ctx = _ctx()

        view = await ConversationMemory(repo).load(_agent(), ctx, client=_client(provider))

        assert provider.invocations == 1  # exactly one summarize call
        assert view.summary == "compressed history"
        assert repo.summary_state["a1:s1"] == ("compressed history", 4)
        # the summarize request carries only system + transcript user message
        request = provider.requests[0]
        assert [m.role for m in request.messages] == ["system", "user"]
        assert "message 0" in request.messages[1].text
        # usage rides ctx.usage so the orchestrator's budget spans the call
        assert ctx.usage.input_tokens == 11 and ctx.usage.output_tokens == 7
        # the view still exposes the FULL history — the engine windows
        assert len(view.history) == 6

    async def test_rolling_summary_extends_previous(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(10))
        repo.summary_state["a1:s1"] = ("first summary", 4)
        provider = MockModelProvider([turn("second summary")])
        ctx = _ctx()

        view = await ConversationMemory(repo).load(_agent(), ctx, client=_client(provider))

        assert view.summary == "second summary"
        assert repo.summary_state["a1:s1"] == ("second summary", 8)  # 4 + evicted 6
        request = provider.requests[0]
        assert "Previous summary:\nfirst summary" in request.messages[1].text
        assert "message 4" in request.messages[1].text  # slice starts at the boundary
        assert "message 3" not in request.messages[1].text  # already summarized

    async def test_degrade_on_model_error_keeps_state(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(6))
        repo.summary_state["a1:s1"] = ("existing summary", 2)
        provider = MockModelProvider([turn(error=ModelError("provider down"))])
        ctx = _ctx()

        view = await ConversationMemory(repo).load(_agent(), ctx, client=_client(provider))

        # no raise, no state change: the segment runs with whatever exists
        assert provider.invocations == 1
        assert view.summary == "existing summary"
        assert repo.summary_state["a1:s1"] == ("existing summary", 2)
        assert len(view.history) == 6

    async def test_abort_propagates(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(6))
        provider = MockModelProvider([turn(error=ModelAbortedError("user cancelled"))])
        ctx = _ctx()

        memory = ConversationMemory(repo)
        try:
            await memory.load(_agent(), ctx, client=_client(provider))
            raise AssertionError("abort must propagate")
        except ModelAbortedError:
            pass
        assert repo.summary_state == {}  # nothing persisted on abort


class TestRebuildView:
    async def test_rebuild_is_read_only_windowed(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(10))
        repo.summary_state["a1:s1"] = ("prior summary", 6)
        ctx = _ctx()

        view = await ConversationMemory(repo).rebuild_view(_agent(), ctx, "a1:s1")

        # existing summary + exactly the window, never a model call
        assert view.summary == "prior summary"
        assert [m.content for m in view.history] == ["message 8", "message 9"]

    async def test_rebuild_memory_off_is_empty(self):
        repo = _Repo()
        repo.seed("a1:s1", _prior_history(4))
        ctx = _ctx()

        view = await ConversationMemory(repo).rebuild_view(_agent(enabled=False), ctx, "a1:s1")

        assert view.history == [] and view.summary is None
