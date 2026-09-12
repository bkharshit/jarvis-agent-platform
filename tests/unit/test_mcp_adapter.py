"""MCP adapter unit tests (S4, plan §6) — the connection seam is faked; no
subprocess, no SDK transport. The SDK types used here (Tool, CallToolResult)
are pure pydantic, so the suite stays hermetic."""

from __future__ import annotations

import asyncio
from typing import Any

import mcp.types
import pytest

from jarvis.config import Settings
from jarvis.domain.agent import ToolBinding
from jarvis.domain.mcp import McpServer, McpStdioConfig
from jarvis.domain.tools import ToolDescriptor
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.mcp.connection import McpServerConnection, _sanitize
from jarvis.tools.mcp.errors import McpResolutionError
from jarvis.tools.mcp.provider import (
    MCP_PREFIX,
    McpTooling,
    McpToolProvider,
    _split_binding_name,
)
from jarvis.tools.registry import InMemoryToolRegistry

SERVER = McpServer(
    id="srv-1",
    name="fixtures",
    config=McpStdioConfig(type="stdio", command="python", args=["server.py"]),
)


# --- the fake seam -----------------------------------------------------------


class FakeSdkClient:
    """Duck-typed SDK v2 Client: list_tools / call_tool, scriptable."""

    def __init__(
        self,
        tools: list[mcp.types.Tool],
        results: dict[str, mcp.types.CallToolResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._tools = tools
        self._results = results or {}
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, *, cache_mode: str = "use") -> mcp.types.ListToolsResult:
        if self._error is not None:
            raise self._error
        return mcp.types.ListToolsResult(tools=list(self._tools))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> mcp.types.CallToolResult:
        self.calls.append((name, arguments))
        if self._error is not None:
            raise self._error
        return self._results[name]


def _tool(raw_name: str, schema: dict | None = None) -> mcp.types.Tool:
    return mcp.types.Tool(
        name=raw_name,
        description=f"the {raw_name} tool",
        input_schema=schema or {"type": "object", "properties": {}},
    )


def _text_result(text: str, *, is_error: bool = False) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)], is_error=is_error
    )


def _wired_connection(client: FakeSdkClient) -> McpServerConnection:
    """A connection with the SDK client already injected (no subprocess)."""
    connection = McpServerConnection(SERVER, connect_timeout=15.0)
    connection._client = client
    return connection


# --- connection mapping -------------------------------------------------------


async def test_descriptors_namespaced_and_gated() -> None:
    connection = _wired_connection(FakeSdkClient([_tool("echo"), _tool("add_numbers")]))
    descriptors = await connection.descriptors()
    assert [d.name for d in descriptors] == ["mcp__fixtures__echo", "mcp__fixtures__add_numbers"]
    assert all(d.annotations["requires_approval"] is True for d in descriptors)
    assert descriptors[0].parameters["type"] == "object"
    assert connection.raw_names["mcp__fixtures__echo"] == "echo"


async def test_descriptors_sanitized_and_collision_first_wins() -> None:
    connection = _wired_connection(FakeSdkClient([_tool("my.tool"), _tool("my.tool")]))
    descriptors = await connection.descriptors()
    names = [d.name for d in descriptors]
    assert names[0] == "mcp__fixtures__my_tool"
    assert names[1] != names[0]  # second collision gets a suffix, never overwrites


async def test_call_joins_text_and_omits_non_text() -> None:
    result = mcp.types.CallToolResult(
        content=[
            mcp.types.TextContent(type="text", text="hello"),
            mcp.types.ImageContent(type="image", data="Zm9v", mime_type="image/png"),
            mcp.types.TextContent(type="text", text="world"),
        ]
    )
    connection = _wired_connection(FakeSdkClient([_tool("echo")], {"echo": result}))
    output = await connection.call("echo", {"text": "hi"})
    assert output == "hello\n[image content omitted]\nworld"


async def test_call_tool_level_error_raises_value_error() -> None:
    connection = _wired_connection(
        FakeSdkClient([_tool("boom")], {"boom": _text_result("it broke", is_error=True)})
    )
    with pytest.raises(ValueError, match="it broke"):
        await connection.call("boom", {})


async def test_call_jsonrpc_error_raises_value_error() -> None:
    from mcp.shared.exceptions import MCPError

    connection = _wired_connection(
        FakeSdkClient([_tool("boom")], error=MCPError(code=1, message="protocol exploded"))
    )
    with pytest.raises(ValueError, match="protocol exploded"):
        await connection.call("boom", {})


async def test_list_failure_is_resolution_error() -> None:
    from mcp.shared.exceptions import MCPError

    connection = _wired_connection(
        FakeSdkClient([], error=MCPError(code=1, message="no tools for you"))
    )
    with pytest.raises(McpResolutionError, match="tools/list failed"):
        await connection.descriptors()


class _RejectingEnterClient:
    """Duck-typed SDK Client whose __aenter__ raises what anyio TaskGroups
    actually raise: the real error wrapped in an ExceptionGroup."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> object:
        raise ExceptionGroup("unhandled errors in a TaskGroup", [self._exc])

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_handshake_failure_names_the_root_cause_not_the_task_group() -> None:
    # Found live (tavily probe): a 401 reject surfaces as MCPError wrapped
    # in anyio's ExceptionGroup — the message must name THAT, not "1
    # sub-exception".
    from mcp.shared.exceptions import MCPError

    client = _RejectingEnterClient(
        MCPError(code=-32000, message="Server returned an error response")
    )

    async def fake_build() -> object:
        return client

    connection = McpServerConnection(SERVER, connect_timeout=15.0)
    connection._build_client = fake_build  # type: ignore[method-assign]
    with pytest.raises(McpResolutionError, match="MCPError: Server returned an error response"):
        await connection.connect()


def test_sanitize_replaces_invalid_chars() -> None:
    seen: set[str] = set()
    assert _sanitize("a b/c", seen) == "a_b_c"
    assert _sanitize("", seen) == "_"


# --- env refs -----------------------------------------------------------------


async def test_missing_env_var_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_WEATHER_TOKEN", raising=False)
    server = McpServer(
        id="srv-2",
        name="weather",
        config=McpStdioConfig(
            type="stdio",
            command="python",
            env={"WEATHER_TOKEN": {"type": "env", "env_var": "MCP_WEATHER_TOKEN"}},
        ),
    )
    connection = McpServerConnection(server, connect_timeout=15.0)
    with pytest.raises(McpResolutionError, match="MCP_WEATHER_TOKEN"):
        await connection.connect()


def test_split_binding_name() -> None:
    assert _split_binding_name("mcp__fixtures__add_numbers") == ("fixtures", "add_numbers")
    assert _split_binding_name("mcp__a__b__c") == ("a", "b__c")
    assert _split_binding_name("calculator") is None
    assert _split_binding_name("mcp__") is None
    assert _split_binding_name("mcp__x__") is None


# --- stored credential refs (ADR 0013) -----------------------------------------


class StubResolver:
    """Duck-typed CredentialResolver: scripted results + call log."""

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.calls: list[tuple[object, str]] = []

    async def resolve(self, principal: object, ref: object) -> object:
        self.calls.append((principal, ref.credential_id))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _http_server(headers: dict[str, object]) -> McpServer:
    from jarvis.domain.mcp import McpHttpConfig

    return McpServer(
        id="srv-http",
        name="httpx",
        config=McpHttpConfig(type="http", url="https://x/mcp", headers=headers),
    )


async def test_stored_header_materializes_through_the_resolver() -> None:
    from jarvis.ports.credential import ResolvedMaterial

    resolver = StubResolver(ResolvedMaterial(type="material", value="Bearer sk_live_1"))
    server = _http_server({"Authorization": {"type": "stored", "credential_id": "cred-1"}})
    connection = McpServerConnection(
        server, connect_timeout=15.0, credential_resolver=resolver, principal="principal-1"
    )
    headers = await connection._resolve_headers(connection._server.config)
    assert headers["Authorization"] == "Bearer sk_live_1"
    # the principal rides through so the tenant-scoped decrypt can happen
    assert resolver.calls == [("principal-1", "cred-1")]


async def test_stored_header_without_a_resolver_is_a_clear_error() -> None:
    server = _http_server({"Authorization": {"type": "stored", "credential_id": "cred-9"}})
    connection = McpServerConnection(server, connect_timeout=15.0)
    with pytest.raises(McpResolutionError, match="no credential resolver"):
        await connection.connect()


async def test_credential_failure_names_the_id_and_the_header() -> None:
    from jarvis.ports.credential import CredentialError

    resolver = StubResolver(CredentialError("credential 'cred-9' not found"))
    server = _http_server({"Authorization": {"type": "stored", "credential_id": "cred-9"}})
    connection = McpServerConnection(server, connect_timeout=15.0, credential_resolver=resolver)
    with pytest.raises(McpResolutionError, match="cred-9.*Authorization|Authorization.*cred-9"):
        await connection.connect()


async def test_stored_env_ref_follows_the_same_path() -> None:
    from jarvis.ports.credential import ResolvedMaterial

    resolver = StubResolver(ResolvedMaterial(type="material", value="tok"))
    server = McpServer(
        id="srv-3",
        name="weather",
        config=McpStdioConfig(
            type="stdio",
            command="python",
            env={"WEATHER_TOKEN": {"type": "stored", "credential_id": "cred-2"}},
        ),
    )
    connection = McpServerConnection(server, connect_timeout=15.0, credential_resolver=resolver)
    env = await connection._resolve_env(connection._server.config)
    assert env["WEATHER_TOKEN"] == "tok"


async def test_env_ref_resolution_on_the_stored_path_is_a_contract_error() -> None:
    from jarvis.ports.credential import ResolvedEnv

    resolver = StubResolver(ResolvedEnv(type="env", env_var="SOMETHING"))
    server = _http_server({"Authorization": {"type": "stored", "credential_id": "cred-1"}})
    connection = McpServerConnection(server, connect_timeout=15.0, credential_resolver=resolver)
    with pytest.raises(McpResolutionError, match="env reference"):
        await connection.connect()


# --- provider grouping ----------------------------------------------------------


async def test_default_factory_threads_resolver_and_principal() -> None:
    from jarvis.domain.auth import Principal

    base = InMemoryToolRegistry()
    base.register(CalculatorTool())
    resolver = StubResolver(None)
    provider = McpToolProvider(
        FakeRepo({}), base, Settings(_env_file=None), credential_resolver=resolver
    )
    principal = Principal(tenant_id="t1", mode="anonymous")
    connection = provider._factory_for(principal)(SERVER)
    # the real connection carries both — resolution happens at its connect
    assert connection._credential_resolver is resolver
    assert connection._principal == principal
    # a custom factory is passed through untouched (the unit-test seam)
    fake = FakeConnection(SERVER, ["echo"])
    custom = McpToolProvider(
        FakeRepo({}), base, Settings(_env_file=None), connection_factory=lambda server: fake
    )
    assert custom._factory_for(principal)(SERVER) is fake


class FakeRepo:
    """get_by_name over an explicit dict — owned rows keyed (tenant, name)."""

    def __init__(self, rows: dict[tuple[str | None, str], McpServer]) -> None:
        self._rows = rows

    async def get_by_name(self, name: str, *, tenant_id: str | None = None) -> McpServer | None:
        owned = self._rows.get((tenant_id, name))
        if owned is not None:
            return owned
        return self._rows.get((None, name))  # shared fallback


class FakeConnection:
    """Duck-typed McpServerConnection for provider tests."""

    def __init__(self, server: McpServer, tools: list[str]) -> None:
        self._tools = tools
        self.server = server
        self.closed = False
        self._raw_names: dict[str, str] = {f"mcp__{server.name}__{t}": t for t in tools}

    @property
    def raw_names(self) -> dict[str, str]:
        return dict(self._raw_names)

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
            for name in self._raw_names
        ]

    async def call(self, raw_tool: str, arguments: dict[str, Any]) -> str:
        return f"{raw_tool} ok"


def _provider(
    rows: dict[tuple[str | None, str], McpServer], factory_map: dict[str, FakeConnection]
) -> McpToolProvider:
    base = InMemoryToolRegistry()
    base.register(CalculatorTool())
    return McpToolProvider(
        FakeRepo(rows),
        base,
        Settings(_env_file=None),
        connection_factory=lambda server: factory_map[server.name],
    )


async def test_provider_registers_bound_tools_only() -> None:
    rows = {(None, "fixtures"): SERVER}
    factory = {"fixtures": FakeConnection(SERVER, ["echo", "add_numbers", "secret_admin"])}
    provider = _provider(rows, factory)
    tooling = await provider.resolve(
        [ToolBinding(name="mcp__fixtures__add_numbers")], tenant_id="t"
    )
    assert isinstance(tooling, McpTooling)
    # discovery is not exposure: the unbound secret_admin is NOT registered
    assert "mcp__fixtures__secret_admin" not in tooling.registry
    assert "mcp__fixtures__add_numbers" in tooling.registry
    # builtins ride along in the same view
    assert "calculator" in tooling.registry
    await tooling.aclose()
    assert factory["fixtures"].closed


async def test_provider_no_mcp_bindings_is_a_noop() -> None:
    rows: dict[tuple[str | None, str], McpServer] = {}
    factory: dict[str, FakeConnection] = {}
    provider = _provider(rows, factory)
    tooling = await provider.resolve([ToolBinding(name="calculator")], tenant_id="t")
    # nothing opened; the view is the base registry itself
    assert len(factory) == 0
    assert "calculator" in tooling.registry
    await tooling.aclose()


async def test_provider_missing_server_raises_and_names_it() -> None:
    provider = _provider({}, {})
    with pytest.raises(McpResolutionError, match="ghost"):
        await provider.resolve([ToolBinding(name="mcp__ghost__echo")], tenant_id="t")


async def test_provider_disabled_server_raises() -> None:
    disabled = SERVER.model_copy(update={"enabled": False})
    rows = {(None, "fixtures"): disabled}
    factory = {"fixtures": FakeConnection(disabled, ["echo"])}
    provider = _provider(rows, factory)
    with pytest.raises(McpResolutionError, match="disabled"):
        await provider.resolve([ToolBinding(name="mcp__fixtures__echo")], tenant_id="t")
    assert factory["fixtures"].closed is False  # never opened


async def test_provider_owned_shadows_shared() -> None:
    shared = SERVER.model_copy(update={"id": "shared"})
    owned = SERVER.model_copy(update={"id": "owned", "name": "fixtures"})
    rows = {(None, "fixtures"): shared, ("tenant-a", "fixtures"): owned}
    factory = {"fixtures": FakeConnection(owned, ["echo"])}
    provider = _provider(rows, factory)
    tooling = await provider.resolve(
        [ToolBinding(name="mcp__fixtures__echo")], tenant_id="tenant-a"
    )
    assert factory["fixtures"].server.id == "owned"  # the owned row was used
    await tooling.aclose()


async def test_provider_groups_two_servers() -> None:
    other = McpServer(id="srv-2", name="weather", config=McpStdioConfig(type="stdio", command="w"))
    rows = {(None, "fixtures"): SERVER, (None, "weather"): other}
    factory = {
        "fixtures": FakeConnection(SERVER, ["echo"]),
        "weather": FakeConnection(other, ["forecast"]),
    }
    provider = _provider(rows, factory)
    tooling = await provider.resolve(
        [ToolBinding(name="mcp__fixtures__echo"), ToolBinding(name="mcp__weather__forecast")],
        tenant_id="t",
    )
    assert {"mcp__fixtures__echo", "mcp__weather__forecast"} <= {
        d.name for d in tooling.registry.descriptors()
    }
    await tooling.aclose()
    assert all(conn.closed for conn in factory.values())


async def test_provider_drifted_tool_is_skipped_not_fatal() -> None:
    rows = {(None, "fixtures"): SERVER}
    factory = {"fixtures": FakeConnection(SERVER, ["echo"])}  # add_numbers gone
    provider = _provider(rows, factory)
    tooling = await provider.resolve(
        [ToolBinding(name="mcp__fixtures__add_numbers"), ToolBinding(name="mcp__fixtures__echo")],
        tenant_id="t",
    )
    assert "mcp__fixtures__add_numbers" not in tooling.registry
    assert "mcp__fixtures__echo" in tooling.registry
    await tooling.aclose()


async def test_provider_disabled_binding_is_ignored() -> None:
    rows = {(None, "fixtures"): SERVER}
    factory: dict[str, FakeConnection] = {}
    provider = _provider(rows, factory)
    tooling = await provider.resolve(
        [ToolBinding(name="mcp__fixtures__echo", enabled=False)], tenant_id="t"
    )
    assert len(factory) == 0
    await tooling.aclose()


def test_mcp_prefix_shape() -> None:
    assert MCP_PREFIX == "mcp__"


def test_no_event_loop_hazards() -> None:
    # the no-op close is a plain coroutine function — awaiting it anywhere works
    from jarvis.tools.mcp.provider import _no_op_close

    asyncio.run(_no_op_close())
