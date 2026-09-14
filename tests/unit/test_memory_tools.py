"""Memory builtin tools (S12, ADR 0016 §3, D46) — over an in-memory
ScratchpadRepo fake, exercising the is_error paths through the real
ToolRuntime envelope."""

from datetime import UTC, datetime

from jarvis.domain.agent import ScratchpadEntry
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext
from jarvis.tools.builtin.memory import (
    DEFAULT_MAX_VALUE_CHARS,
    MemoryDeleteTool,
    MemoryGetTool,
    MemoryPutTool,
)
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime


class _MemoryStore:
    """In-memory ScratchpadRepo fake — the same natural-key semantics AND
    the D29 tenant filter (a foreign tenant reads absence)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[str, str]] = {}

    async def get(self, agent_id, session_id, key, *, tenant_id=None):
        row = self.rows.get((agent_id, session_id, key))
        if row is None:
            return None
        value, row_tenant = row
        if tenant_id is not None and row_tenant != tenant_id:
            return None
        return ScratchpadEntry(
            agent_id=agent_id,
            session_id=session_id,
            key=key,
            value=value,
            updated_at=datetime.now(UTC),
        )

    async def put(self, agent_id, session_id, key, value, *, tenant_id=None):
        effective = tenant_id or "default"
        self.rows[(agent_id, session_id, key)] = (value, effective)
        return ScratchpadEntry(
            agent_id=agent_id,
            session_id=session_id,
            key=key,
            value=value,
            updated_at=datetime.now(UTC),
        )

    async def delete(self, agent_id, session_id, key, *, tenant_id=None):
        row = self.rows.get((agent_id, session_id, key))
        if row is None:
            return False
        if tenant_id is not None and row[1] != tenant_id:
            return False
        del self.rows[(agent_id, session_id, key)]
        return True


def _tools():
    store = _MemoryStore()
    tools = (MemoryGetTool(store), MemoryPutTool(store), MemoryDeleteTool(store))
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return store, ToolRuntime(registry)


def _context(config=None, **kw):
    base = dict(run_id="r1", agent_id="a1", session_id="s1")
    base.update(kw)
    return ToolContext(config=config or {}, **base)


def _call(name, **arguments):
    return ToolCall(id="c1", name=name, arguments=arguments)


class TestMemoryRoundtrip:
    async def test_put_then_get(self):
        _, runtime = _tools()
        result = await runtime.execute(_call("memory_put", key="notes", value="first"), _context())
        assert not result.is_error
        result = await runtime.execute(_call("memory_get", key="notes"), _context())
        assert result.output == "first"

    async def test_put_overwrites(self):
        _, runtime = _tools()
        await runtime.execute(_call("memory_put", key="notes", value="first"), _context())
        await runtime.execute(_call("memory_put", key="notes", value="second"), _context())
        result = await runtime.execute(_call("memory_get", key="notes"), _context())
        assert result.output == "second"

    async def test_get_unset_key_is_error(self):
        _, runtime = _tools()
        result = await runtime.execute(_call("memory_get", key="missing"), _context())
        assert result.is_error
        assert "not set" in result.output

    async def test_delete_removes_then_reports_unset(self):
        _, runtime = _tools()
        await runtime.execute(_call("memory_put", key="notes", value="v"), _context())
        result = await runtime.execute(_call("memory_delete", key="notes"), _context())
        assert not result.is_error
        result = await runtime.execute(_call("memory_delete", key="notes"), _context())
        assert result.is_error and "not set" in result.output

    async def test_keys_are_scoped_by_agent_and_session(self):
        _, runtime = _tools()
        await runtime.execute(_call("memory_put", key="k", value="v"), _context())
        other_agent = _context(agent_id="a2")
        other_session = _context(session_id="s2")
        for context in (other_agent, other_session):
            result = await runtime.execute(_call("memory_get", key="k"), context)
            assert result.is_error


class TestSessionRequirement:
    async def test_no_session_is_error_for_every_tool(self):
        _, runtime = _tools()
        for call in (
            _call("memory_get", key="k"),
            _call("memory_put", key="k", value="v"),
            _call("memory_delete", key="k"),
        ):
            result = await runtime.execute(call, _context(session_id=None))
            assert result.is_error
            assert "requires a session" in result.output


class TestValueCap:
    async def test_default_cap_rejects_oversized_value(self):
        _, runtime = _tools()
        result = await runtime.execute(
            _call("memory_put", key="k", value="x" * (DEFAULT_MAX_VALUE_CHARS + 1)),
            _context(),
        )
        assert result.is_error
        assert "limit" in result.output

    async def test_binding_config_overrides_the_cap(self):
        _, runtime = _tools()
        result = await runtime.execute(
            _call("memory_put", key="k", value="x" * 11), _context(config={"max_value_chars": 10})
        )
        assert result.is_error
        result = await runtime.execute(
            _call("memory_put", key="k", value="x" * 10), _context(config={"max_value_chars": 10})
        )
        assert not result.is_error


class TestTenancy:
    async def test_tenant_id_scopes_reads_and_writes(self):
        _, runtime = _tools()
        await runtime.execute(_call("memory_put", key="k", value="v"), _context(tenant_id="t1"))
        # a foreign tenant reads absence (D29)
        result = await runtime.execute(_call("memory_get", key="k"), _context(tenant_id="t2"))
        assert result.is_error
        # the owning tenant reads its row
        result = await runtime.execute(_call("memory_get", key="k"), _context(tenant_id="t1"))
        assert result.output == "v"
        # and a foreign delete touches nothing
        result = await runtime.execute(_call("memory_delete", key="k"), _context(tenant_id="t2"))
        assert result.is_error
        result = await runtime.execute(_call("memory_get", key="k"), _context(tenant_id="t1"))
        assert result.output == "v"
