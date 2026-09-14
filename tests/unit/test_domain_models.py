"""Domain model validation: messages, usage, agents, execution state."""

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ConversationMemoryState,
    EnvCredentialRef,
    MemoryConfig,
    ModelRef,
    StoredCredentialRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.execution import (
    CancellationToken,
    ExecutionCancelled,
    ExecutionContext,
    RunResult,
)
from jarvis.domain.message import Message, TextPart, Usage, assistant, tool_result


class TestUsage:
    def test_plus_accumulates(self):
        a = Usage(input_tokens=1, output_tokens=2, extra={"cached": 3})
        b = Usage(input_tokens=10, output_tokens=20, extra={"cached": 4})
        total = a.plus(b)
        assert total.input_tokens == 11
        assert total.output_tokens == 22
        assert total.extra == {"cached": 7}

    def test_plus_returns_new_instance(self):
        a = Usage(input_tokens=1)
        total = a.plus(a)
        assert a.input_tokens == 1
        assert total.input_tokens == 2


class TestMessage:
    def test_text_from_string(self):
        assert Message(role="user", content="hello").text == "hello"

    def test_text_from_parts(self):
        msg = Message(role="user", content=[TextPart(text="a"), TextPart(text="b")])
        assert msg.text == "ab"

    def test_empty_content_defaults(self):
        msg = assistant()
        assert msg.text == ""
        assert msg.tool_calls is None

    def test_tool_result_helper(self):
        msg = tool_result("tc1", "calculator", "42")
        assert msg.role == "tool"
        assert msg.tool_call_id == "tc1"
        assert msg.name == "calculator"

    def test_forbids_unknown_fields(self):
        with pytest.raises(ValidationError):
            Message(role="user", content="x", bogus=1)


class TestAgentDefinition:
    def _definition(self, **overrides):
        base = dict(
            id="a1",
            name="demo",
            model=ModelRef(provider="mock", model="mock-1"),
            strategy=StrategyConfig(type="function_calling"),
        )
        base.update(overrides)
        return AgentDefinition(**base)

    def test_minimal_definition(self):
        agent = self._definition()
        assert agent.max_iterations == 8
        assert agent.temperature == 0.7
        assert agent.memory.enabled is False
        assert agent.tools == []

    def test_blank_name_rejected(self):
        with pytest.raises(ValidationError):
            self._definition(name="   ")

    def test_max_iterations_bounds(self):
        with pytest.raises(ValidationError):
            self._definition(max_iterations=0)
        with pytest.raises(ValidationError):
            self._definition(max_iterations=33)

    def test_strategy_type_is_a_free_string(self):
        # D36: the domain no longer closes the set — arbitrary types are
        # valid domain data; unknown-type protection lives at the create
        # boundary (API 422 / CLI error, against the live registry).
        agent = self._definition(strategy=StrategyConfig(type="plan_execute"))
        assert agent.strategy.type == "plan_execute"

    def test_strategy_type_not_blank(self):
        with pytest.raises(ValidationError):
            self._definition(strategy=StrategyConfig(type=""))

    def test_enabled_tools_filters(self):
        agent = self._definition(
            tools=[
                ToolBinding(name="calc", enabled=True),
                ToolBinding(name="off", enabled=False),
            ]
        )
        assert [t.name for t in agent.enabled_tools()] == ["calc"]

    def test_version_snapshot_is_full_definition(self):
        agent = self._definition()
        version = AgentVersion(id="v1", agent_id="a1", version=1, snapshot=agent)
        assert version.snapshot.name == "demo"
        assert version.snapshot.model_dump() == agent.model_dump()


class TestMemoryConfig:
    """S12 (D45): the strategy field is additive — old snapshots parse."""

    def test_pre_s12_snapshot_parses_with_window_default(self):
        # A pre-S12 MemoryConfig JSON dict (no strategy key) — exactly what
        # every persisted agent version snapshot carries.
        old = {"enabled": True, "max_messages": 10, "session_key": None}
        memory = MemoryConfig.model_validate(old)
        assert memory.strategy == "window"

    def test_strategy_literal_closed(self):
        with pytest.raises(ValidationError):
            MemoryConfig(enabled=True, strategy="vector")  # S8, not yet
        with pytest.raises(ValidationError):
            MemoryConfig(enabled=True, strategy="summarize ")  # no trailing space

    def test_summarize_strategy_round_trips(self):
        memory = MemoryConfig(enabled=True, max_messages=5, strategy="summarize")
        assert MemoryConfig.model_validate(memory.model_dump()).strategy == "summarize"


class TestConversationMemoryState:
    """S12 (D45): the rolling-summary state persisted on conversations."""

    def test_defaults(self):
        state = ConversationMemoryState()
        assert state.summary is None
        assert state.summarized_count == 0

    def test_rejects_negative_count(self):
        with pytest.raises(ValidationError):
            ConversationMemoryState(summary="s", summarized_count=-1)


class TestCancellationToken:
    def test_trigger_then_raise(self):
        token = CancellationToken()
        assert not token.triggered
        token.trigger("user request")
        assert token.triggered
        with pytest.raises(ExecutionCancelled, match="user request"):
            token.raise_if_triggered()

    def test_first_reason_wins(self):
        token = CancellationToken()
        token.trigger("first")
        token.trigger("second")
        assert token.reason == "first"

    def test_child_fires_with_parent(self):
        parent = CancellationToken()
        child = parent.child()
        parent.trigger("stop")
        assert child.triggered
        assert child.reason == "stop"

    def test_child_can_fire_alone(self):
        parent = CancellationToken()
        child = parent.child()
        child.trigger("tool timeout")
        assert child.triggered
        assert not parent.triggered

    def test_child_created_after_trigger_fires_immediately(self):
        parent = CancellationToken()
        parent.trigger("done")
        assert parent.child().triggered


class TestExecutionContext:
    def test_check_limits_passes_when_clear(self):
        ctx = ExecutionContext(run_id="r", agent_id="a", agent_version_id="v")
        ctx.check_limits()

    def test_check_limits_raises_when_cancelled(self):
        ctx = ExecutionContext(run_id="r", agent_id="a", agent_version_id="v")
        ctx.cancel.trigger("bye")
        with pytest.raises(ExecutionCancelled):
            ctx.check_limits()

    def test_check_limits_raises_past_deadline(self):
        from datetime import UTC, timedelta
        from datetime import datetime as dt

        ctx = ExecutionContext(
            run_id="r",
            agent_id="a",
            agent_version_id="v",
            deadline=dt.now(UTC) - timedelta(seconds=1),
        )
        with pytest.raises(ExecutionCancelled, match="deadline"):
            ctx.check_limits()


class TestRunResult:
    def test_defaults(self):
        result = RunResult(run_id="r", agent_id="a", status="succeeded")
        assert result.final_message is None
        assert result.iterations == 0
        assert result.event_cursor is None
        assert result.finished_at is None

    def test_queued_status_is_valid(self):
        # S1: runs are enqueued before a worker claims them.
        result = RunResult(run_id="r", agent_id="a", status="queued")
        assert result.status == "queued"


class TestCredentialRef:
    """S2 (ADR 0006): ModelRef carries credential *references* — an env-var
    name or a stored credential id — never material."""

    def test_env_ref_roundtrip(self):
        ref = ModelRef(
            provider="openai_compatible",
            model="gpt-4o-mini",
            credential_ref=EnvCredentialRef(type="env", env_var="OPENAI_API_KEY"),
        )
        dumped = ref.model_dump()
        assert dumped["credential_ref"] == {"type": "env", "env_var": "OPENAI_API_KEY"}
        assert ModelRef.model_validate(dumped).credential_ref == ref.credential_ref

    def test_stored_ref(self):
        ref = ModelRef(
            provider="openai_compatible",
            model="gpt-4o-mini",
            credential_ref=StoredCredentialRef(type="stored", credential_id="cred_1"),
        )
        assert ref.credential_ref.type == "stored"
        assert ref.credential_ref.credential_id == "cred_1"

    def test_discriminator_required(self):
        with pytest.raises(ValidationError):
            ModelRef(
                provider="openai_compatible",
                model="m",
                credential_ref={"env_var": "OPENAI_API_KEY"},  # type: ignore[dict-item]
            )

    def test_unknown_discriminator_rejected(self):
        with pytest.raises(ValidationError):
            ModelRef(
                provider="openai_compatible",
                model="m",
                credential_ref={"type": "plaintext", "value": "sk-..."},  # type: ignore[dict-item]
            )

    def test_api_key_env_is_gone(self):
        # The old field was removed (extra="forbid" makes stale writers fail
        # loudly) — this is the ADR 0006 §S2-migration shape change.
        with pytest.raises(ValidationError):
            ModelRef(provider="mock", model="m", api_key_env="OPENAI_API_KEY")

    def test_absent_ref_defaults_none(self):
        ref = ModelRef(provider="mock", model="m")
        assert ref.credential_ref is None
