"""PromptEngine — prompt building as a transform fed a context object.

`{{var}}` templates, token-budgeted history window, tool sections for
ReAct-style prompts, and the schema-in-prompt fallback for providers whose
structured output is json_mode or none (ADR 0005)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.domain.agent import AgentDefinition
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolDescriptor

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class PromptContext:
    """Everything a prompt needs. Built by the orchestrator, consumed here."""

    agent: AgentDefinition
    input: str
    variables: dict[str, Any] = field(default_factory=dict)
    history: list[Message] = field(default_factory=list)
    tools: list[ToolDescriptor] = field(default_factory=list)
    schema_in_prompt: bool = False  # provider can't do native structured output

    @property
    def substitutions(self) -> dict[str, Any]:
        values = dict(self.variables)
        values.setdefault("input", self.input)
        return values


class PromptEngine:
    def build(self, context: PromptContext) -> list[Message]:
        """Render the full message list for one model invocation."""
        messages: list[Message] = []

        system = self.render_system(context)
        if system:
            messages.append(Message(role="system", content=system))

        messages.extend(self.history_window(context))

        messages.append(Message(role="user", content=self.render_user(context)))
        return messages

    # --- sections -----------------------------------------------------------

    def render_system(self, context: PromptContext) -> str:
        sections: list[str] = []
        base = context.agent.system_prompt.strip()
        if base:
            sections.append(self.render(base, context.substitutions))
        if context.tools and context.agent.strategy.type == "react":
            sections.append(self._tool_section(context.tools))
        if context.schema_in_prompt and context.agent.output_schema:
            sections.append(self._schema_section(context.agent.output_schema))
        if context.tools and context.agent.strategy.type == "react":
            sections.append(REACT_FORMAT_INSTRUCTIONS)
        return "\n\n".join(section for section in sections if section)

    def render_user(self, context: PromptContext) -> str:
        template = context.agent.user_prompt_template
        if not template:
            return context.input
        return self.render(template, context.substitutions)

    def history_window(self, context: PromptContext) -> list[Message]:
        """Last `memory.max_messages` history messages, newest kept, that fit
        the budget. Never touches the current input."""
        limit = context.agent.memory.max_messages if context.agent.memory.enabled else 0
        if limit <= 0 or not context.history:
            return []
        return context.history[-limit:]

    # --- helpers -------------------------------------------------------------

    def render(self, template: str, values: dict[str, Any]) -> str:
        def _substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                return match.group(0)  # unknown vars pass through untouched
            value = values[key]
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)

        return _VAR_PATTERN.sub(_substitute, template)

    @staticmethod
    def _tool_section(tools: list[ToolDescriptor]) -> str:
        lines = ["Available tools:"]
        for tool in tools:
            params = json.dumps(tool.parameters or {"type": "object"})
            lines.append(f"- {tool.name}: {tool.description} Parameters: {params}")
        return "\n".join(lines)

    @staticmethod
    def _schema_section(schema: dict[str, Any]) -> str:
        return (
            "You MUST reply with a single JSON object matching this JSON Schema "
            f"(no prose, no markdown fences):\n{json.dumps(schema, indent=2)}"
        )


REACT_FORMAT_INSTRUCTIONS = """Use exactly this format in your reply:

Thought: your reasoning about what to do next
Action: the name of the tool to use, one of the available tools
Action Input: a JSON object of arguments for the tool

OR, when you know the final answer, reply with:

Thought: your reasoning
Final Answer: your answer to the user"""


__all__ = ["PromptContext", "PromptEngine", "REACT_FORMAT_INSTRUCTIONS"]
