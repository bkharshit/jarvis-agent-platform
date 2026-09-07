"""RaisePluginStrategy — the malformed-plugin probe (S3).

Always raises in `step`; the acceptance line it locks is that a broken
plugin can never crash a run: the orchestrator maps the raise to one
persisted terminal `run.failed` with `error_kind="strategy"` (D36)."""

from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolDescriptor
from jarvis.ports.events import EventSink
from jarvis.ports.model import ModelClient
from jarvis.ports.strategy import StepOutcome


class RaisePluginStrategy:
    @property
    def name(self) -> str:
        return "raise_plugin"

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[ToolDescriptor],
        sink: EventSink,
    ) -> StepOutcome:
        raise RuntimeError("raise_plugin always raises (fixture)")
