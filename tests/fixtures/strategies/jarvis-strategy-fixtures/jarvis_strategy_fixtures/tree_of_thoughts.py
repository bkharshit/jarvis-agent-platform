"""TreeOfThoughtsStrategy — a second sample plugin (S3 walkthrough).

Diverge → evaluate → exploit, as transcript-driven phases (stateless;
per-run state lives in the transcript, never on `self`):

- Phase DIVERGE: no option marker in any assistant message yet → one
  model invocation asking for N distinct candidate approaches, each
  prefixed with the option marker, returned as a **think step** (empty
  `tool_calls` — the orchestrator persists the assistant message and
  continues the loop).
- Phase EVALUATE: options exist but no picked marker yet → one model
  invocation comparing the options and naming a winner (picked marker),
  also a think step.
- Phase EXECUTE: follow the picked approach (tools ride along). A reply
  containing the done marker ends the run (`FinishStep`); tool calls are
  handed back; plain text is a think step.

Markers match as substrings — real models do not reliably obey
marker-placement instructions. Params via `ctx.metadata["strategy_params"]`
(num_options / options_marker / picked_marker / done_marker).
"""

from __future__ import annotations

from typing import Any

from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message
from jarvis.domain.message import developer as developer_message
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.types import ModelRequest
from jarvis.ports.events import EventSink
from jarvis.ports.model import ModelClient
from jarvis.ports.strategy import FinishStep, StepOutcome, ToolCallsStep

from .invoke import invoke as _invoke

_DIVERGE_INSTRUCTION = (
    "You are in a tree-of-thoughts loop. Propose {num_options} genuinely "
    "DIFFERENT candidate approaches for the user's request (not variations "
    "of one approach). Put each on its own block, starting with the option "
    "marker and a letter: '{options_marker} A: ...', '{options_marker} B: ...'."
)
_EVALUATE_INSTRUCTION = (
    "You are the evaluator in a tree-of-thoughts loop. Compare the options "
    "above for correctness, simplicity, and risk. Then pick exactly one "
    "winner: start your verdict line with the picked marker, the winning "
    "option letter, and a one-line reason."
)
_EXECUTE_INSTRUCTION = (
    "You are the executor in a tree-of-thoughts loop. The transcript names "
    "the winning approach ({picked_marker}). Follow it (using tools if "
    "appropriate). When the task is complete, include the done marker "
    "followed by a one-line summary."
)


class TreeOfThoughtsStrategy:
    @property
    def name(self) -> str:
        return "tree_of_thoughts"

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[ToolDescriptor],
        sink: EventSink,
    ) -> StepOutcome:
        params: dict[str, Any] = ctx.metadata.get("strategy_params") or {}
        num_options = int(params.get("num_options", 3))
        options_marker = str(params.get("options_marker", "OPTION"))
        picked_marker = str(params.get("picked_marker", "PICKED:"))
        done_marker = str(params.get("done_marker", "DONE:"))

        assistant_texts = [m.text for m in messages if m.role == "assistant"]
        last_text = assistant_texts[-1] if assistant_texts else ""

        if done_marker in last_text:
            return FinishStep(assistant_message=messages[-1], finish_reason="stop")

        if not any(options_marker in text for text in assistant_texts):
            # Phase DIVERGE — candidate approaches, one per option marker.
            response = await _invoke(
                ctx,
                ModelRequest(
                    model="",
                    messages=[
                        *messages,
                        developer_message(
                            _DIVERGE_INSTRUCTION.format(
                                num_options=num_options, options_marker=options_marker
                            )
                        ),
                    ],
                    temperature=ctx.temperature,
                ),
                client,
                sink,
            )
            return ToolCallsStep(assistant_message=response.message)  # think step

        if not any(picked_marker in text for text in assistant_texts):
            # Phase EVALUATE — compare, then name the winner.
            response = await _invoke(
                ctx,
                ModelRequest(
                    model="",
                    messages=[
                        *messages,
                        developer_message(_EVALUATE_INSTRUCTION),
                    ],
                    temperature=ctx.temperature,
                ),
                client,
                sink,
            )
            return ToolCallsStep(assistant_message=response.message)  # think step

        # Phase EXECUTE — tools ride along; a DONE reply ends the run.
        response = await _invoke(
            ctx,
            ModelRequest(
                model="",
                messages=[
                    *messages,
                    developer_message(_EXECUTE_INSTRUCTION.format(picked_marker=picked_marker)),
                ],
                tools=tools or None,
                temperature=ctx.temperature,
            ),
            client,
            sink,
        )
        if done_marker in response.message.text:
            return FinishStep(assistant_message=response.message, finish_reason="stop")
        if response.message.tool_calls:
            return ToolCallsStep(
                assistant_message=response.message, tool_calls=response.message.tool_calls
            )
        return ToolCallsStep(assistant_message=response.message)  # think step
