"""HTTP GET builtin — allow-list enforced. No allow-list, no request."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.tools.base import BaseTool

DESCRIPTOR = ToolDescriptor(
    name="http_get",
    description="Fetch a URL over HTTPS/HTTP and return the response body as text.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL to fetch"}},
        "required": ["url"],
    },
)


class HttpGetTool(BaseTool):
    """`allowed_hosts` (exact hostnames, or '*' to allow any — tests only)
    comes from the tool binding config: `config: {allowed_hosts: [...]}`.
    An empty allow-list refuses every request — allow-lists are opt-in."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(DESCRIPTOR, config)

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        url = str(arguments["url"])
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"not an absolute http(s) URL: {url!r}")
        allowed = self.config.get("allowed_hosts") or context.config.get("allowed_hosts") or []
        if "*" not in allowed and parsed.hostname not in allowed:
            raise ValueError(f"host {parsed.hostname!r} is not in the allow-list {list(allowed)}")
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url)
        body = response.text
        limit = int(self.config.get("max_chars", 20_000))
        if len(body) > limit:
            body = body[:limit] + f"\n... [truncated at {limit} chars]"
        return f"HTTP {response.status_code}\n{body}"


__all__ = ["DESCRIPTOR", "HttpGetTool"]
