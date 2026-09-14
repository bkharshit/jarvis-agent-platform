"""PromptEngine: templates, variables, history window, tool sections,
schema-in-prompt fallback."""

from jarvis.domain.agent import (
    AgentDefinition,
    MemoryConfig,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.message import Message, TextPart
from jarvis.domain.tools import ToolDescriptor
from jarvis.prompt.engine import PromptContext, PromptEngine


def _agent(**overrides):
    base = dict(
        id="a1",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        system_prompt="You are helpful.",
    )
    base.update(overrides)
    return AgentDefinition(**base)


CALC = ToolDescriptor(name="calculator", description="math", parameters={"type": "object"})


class TestTemplates:
    def test_plain_input_without_template(self):
        engine = PromptEngine()
        messages = engine.build(PromptContext(agent=_agent(), input="hello"))
        assert [m.role for m in messages] == ["system", "user"]
        assert messages[1].content == "hello"

    def test_user_prompt_template_with_input_var(self):
        agent = _agent(user_prompt_template="Summarize: {{input}}")
        messages = PromptEngine().build(PromptContext(agent=agent, input="abc"))
        assert messages[-1].content == "Summarize: abc"

    def test_variables_substituted_in_system_and_user(self):
        agent = _agent(
            system_prompt="You are {{tone}}.",
            user_prompt_template="Topic: {{topic}}. {{input}}",
        )
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="q", variables={"tone": "terse", "topic": "kafka"})
        )
        assert messages[0].content == "You are terse."
        assert messages[-1].content == "Topic: kafka. q"

    def test_unknown_variable_passes_through(self):
        agent = _agent(system_prompt="Keep {{unknown}} intact")
        messages = PromptEngine().build(PromptContext(agent=agent, input="x"))
        assert messages[0].content == "Keep {{unknown}} intact"

    def test_dict_variable_json_rendered(self):
        agent = _agent(system_prompt="Schema: {{schema}}")
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="x", variables={"schema": {"a": 1}})
        )
        assert '{"a": 1}' in messages[0].content

    def test_empty_system_prompt_omitted(self):
        agent = _agent(system_prompt="")
        messages = PromptEngine().build(PromptContext(agent=agent, input="x"))
        assert [m.role for m in messages] == ["user"]


class TestHistory:
    def _history(self, count):
        return [Message(role="user", content=f"m{i}") for i in range(count)]

    def test_history_included_when_memory_enabled(self):
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=3))
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="now", history=self._history(5))
        )
        # system + 3 history + user
        assert [m.content for m in messages] == ["You are helpful.", "m2", "m3", "m4", "now"]

    def test_history_omitted_when_memory_disabled(self):
        agent = _agent()
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="now", history=self._history(5))
        )
        assert len(messages) == 2

    def test_history_window_takes_last_n(self):
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=2))
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="now", history=self._history(10))
        )
        assert [m.content for m in messages] == ["You are helpful.", "m8", "m9", "now"]


class TestToolSections:
    def test_react_gets_tool_section_and_format(self):
        agent = _agent(
            strategy=StrategyConfig(type="react"),
            tools=[ToolBinding(name="calculator")],
        )
        messages = PromptEngine().build(PromptContext(agent=agent, input="x", tools=[CALC]))
        system = messages[0].content
        assert "Available tools:" in system
        assert "- calculator: math" in system
        assert "Final Answer:" in system

    def test_function_calling_gets_no_tool_section(self):
        agent = _agent(strategy=StrategyConfig(type="function_calling"))
        messages = PromptEngine().build(PromptContext(agent=agent, input="x", tools=[CALC]))
        assert "Available tools:" not in messages[0].content


class TestSchemaInPrompt:
    def test_schema_rendered_when_provider_cannot_do_native(self):
        agent = _agent(output_schema={"type": "object", "properties": {"a": {"type": "integer"}}})
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="x", schema_in_prompt=True)
        )
        assert "JSON Schema" in messages[0].content
        assert '"type": "integer"' in messages[0].content

    def test_schema_not_rendered_for_native_providers(self):
        agent = _agent(output_schema={"type": "object"})
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="x", schema_in_prompt=False)
        )
        assert messages[0].content == "You are helpful."


class TestContentParts:
    def test_multipart_user_message_passes_through(self):
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=5))
        history = [Message(role="user", content=[TextPart(text="part1"), TextPart(text="part2")])]
        messages = PromptEngine().build(PromptContext(agent=agent, input="now", history=history))
        assert isinstance(messages[1].content, list)


class TestMemorySummary:
    """S12 (D45): the rolling summary injects between system and window."""

    def _history(self, count):
        return [Message(role="user", content=f"m{i}") for i in range(count)]

    def test_summary_injected_before_window(self):
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=2))
        messages = PromptEngine().build(
            PromptContext(
                agent=agent,
                input="now",
                history=self._history(10),
                memory_summary="the user discussed quotas",
            )
        )
        assert [m.content for m in messages] == [
            "You are helpful.",
            "Summary of the earlier conversation:\nthe user discussed quotas",
            "m8",
            "m9",
            "now",
        ]
        assert [m.role for m in messages] == ["system", "system", "user", "user", "user"]

    def test_no_summary_field_means_no_message(self):
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=2))
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="now", history=self._history(10))
        )
        assert len(messages) == 4  # window default byte-identical

    def test_empty_summary_string_omitted(self):
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=1))
        messages = PromptEngine().build(
            PromptContext(agent=agent, input="now", history=self._history(3), memory_summary="")
        )
        assert [m.content for m in messages] == ["You are helpful.", "m2", "now"]

    def test_summary_without_system_prompt_leads(self):
        agent = _agent(system_prompt="", memory=MemoryConfig(enabled=True, max_messages=1))
        messages = PromptEngine().build(
            PromptContext(
                agent=agent, input="now", history=self._history(3), memory_summary="prior talk"
            )
        )
        assert messages[0].content == "Summary of the earlier conversation:\nprior talk"
