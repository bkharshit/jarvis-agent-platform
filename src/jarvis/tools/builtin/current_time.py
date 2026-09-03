"""Current-time builtin."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.tools.base import BaseTool

DESCRIPTOR = ToolDescriptor(
    name="current_time",
    description="Get the current date and time, optionally in a named IANA timezone.",
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name, e.g. 'Europe/Berlin'. Defaults to UTC.",
            }
        },
    },
)


class CurrentTimeTool(BaseTool):
    def __init__(self) -> None:
        super().__init__(DESCRIPTOR)

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        timezone = str(arguments.get("timezone") or "UTC")
        try:
            now = datetime.now(ZoneInfo(timezone) if timezone != "UTC" else UTC)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {timezone!r}") from exc
        return now.isoformat()


__all__ = ["CurrentTimeTool", "DESCRIPTOR"]
