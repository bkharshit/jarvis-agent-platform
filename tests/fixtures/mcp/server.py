"""Fixture MCP stdio server (S4) — two deliberately simple tools.

Launched as `[sys.executable, tests/fixtures/mcp/server.py]` by the
acceptance e2e and the probe route. Uses the same SDK the adapter pins
(`mcp>=2.0,<3`) so the fixture tracks the real v2 surface.
"""

from __future__ import annotations

import mcp.types
from mcp.server import Server, ServerRequestContext


def build_server() -> Server:
    async def on_list_tools(
        ctx: ServerRequestContext[None], params: mcp.types.PaginatedRequestParams | None
    ) -> mcp.types.ListToolsResult:
        return mcp.types.ListToolsResult(
            tools=[
                mcp.types.Tool(
                    name="echo",
                    description="Echo the given text back.",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                ),
                mcp.types.Tool(
                    name="add_numbers",
                    description="Add two numbers and return the sum.",
                    input_schema={
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"],
                    },
                ),
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[None], params: mcp.types.CallToolRequestParams
    ) -> mcp.types.CallToolResult:
        args = params.arguments or {}
        if params.name == "echo":
            return mcp.types.CallToolResult(
                content=[mcp.types.TextContent(type="text", text=str(args.get("text", "")))]
            )
        if params.name == "add_numbers":
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text", text=str(float(args.get("a", 0)) + float(args.get("b", 0)))
                    )
                ]
            )
        raise ValueError(f"unknown tool: {params.name}")

    return Server("jarvis-mcp-fixture", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


if __name__ == "__main__":
    import anyio
    from mcp.server.stdio import stdio_server

    app = build_server()

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    anyio.run(serve)
