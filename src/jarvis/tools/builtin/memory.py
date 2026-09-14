"""Working-memory builtin tools (S12, ADR 0016 §3, D46).

Three tools over the ScratchpadRepo — a per-(agent, session) KV side
channel the model round-trips structured state through. The store is
injected at construction (the AppContainer wires the SQL repo); binding
config rides `context.config` like every tool, so a binding can tighten
`max_value_chars` per agent.

The scratchpad is a TOOL surface, orthogonal to conversation memory: it
keys on (agent_id, session_id) from the ToolContext, never on
memory.enabled — a memory-less agent can still bind it. A run without a
session has nothing to key on, so the tools fail honestly through the
recoverable ToolResult path (raise; the ToolRuntime owns is_error
conversion) — never an exception past the runtime.
"""

from __future__ import annotations

from typing import Any

from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.ports.repository import ScratchpadRepo
from jarvis.tools.base import BaseTool

DEFAULT_MAX_VALUE_CHARS = 16_000


def _session(context: ToolContext) -> str:
    if not context.session_id:
        raise ValueError("the scratchpad requires a session — memory tools need session_id")
    return context.session_id


def _max_value_chars(context: ToolContext) -> int:
    return int(context.config.get("max_value_chars", DEFAULT_MAX_VALUE_CHARS))


_GET_DESCRIPTOR = ToolDescriptor(
    name="memory_get",
    description=(
        "Read a value from this session's working memory. Returns 'not set' as an error when "
        "the key was never stored."
    ),
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "the key to read"}},
        "required": ["key"],
    },
)

_PUT_DESCRIPTOR = ToolDescriptor(
    name="memory_put",
    description=(
        "Store a value in this session's working memory under a key (overwrites an existing "
        "value for the same key)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "the key to store under"},
            "value": {"type": "string", "description": "the value to store (plain text)"},
        },
        "required": ["key", "value"],
    },
)

_DELETE_DESCRIPTOR = ToolDescriptor(
    name="memory_delete",
    description="Delete a key from this session's working memory.",
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "the key to delete"}},
        "required": ["key"],
    },
)


class MemoryGetTool(BaseTool):
    def __init__(self, store: ScratchpadRepo) -> None:
        super().__init__(_GET_DESCRIPTOR)
        self._store = store

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        session_id = _session(context)
        entry = await self._store.get(
            context.agent_id, session_id, str(arguments["key"]), tenant_id=context.tenant_id
        )
        if entry is None:
            raise ValueError(f"key {arguments['key']!r} is not set")
        return entry.value


class MemoryPutTool(BaseTool):
    def __init__(self, store: ScratchpadRepo) -> None:
        super().__init__(_PUT_DESCRIPTOR)
        self._store = store

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        session_id = _session(context)
        value = str(arguments["value"])
        cap = _max_value_chars(context)
        if len(value) > cap:
            raise ValueError(f"value exceeds the {cap}-character limit (got {len(value)})")
        entry = await self._store.put(
            context.agent_id,
            session_id,
            str(arguments["key"]),
            value,
            tenant_id=context.tenant_id,
        )
        return f"stored under {entry.key!r}"


class MemoryDeleteTool(BaseTool):
    def __init__(self, store: ScratchpadRepo) -> None:
        super().__init__(_DELETE_DESCRIPTOR)
        self._store = store

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        session_id = _session(context)
        deleted = await self._store.delete(
            context.agent_id, session_id, str(arguments["key"]), tenant_id=context.tenant_id
        )
        if not deleted:
            raise ValueError(f"key {arguments['key']!r} is not set")
        return f"deleted {arguments['key']!r}"


__all__ = [
    "DEFAULT_MAX_VALUE_CHARS",
    "MemoryDeleteTool",
    "MemoryGetTool",
    "MemoryPutTool",
]
