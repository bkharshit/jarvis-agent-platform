"""TreeOfThoughtsStrategy — a second sample plugin (S3 walkthrough).

Diverge → evaluate → exploit, as transcript-driven phases (stateless;
per-run state lives in the transcript, never on `self`):

- Phase DIVERGE (no assistant reply yet): one model invocation asking for
  N distinct candidate approaches, each prefixed with the option marker,
  returned as a **think step** (empty `tool_calls` — the orchestrator
  persists the assistant message and continues the loop).
- Phase EVALUATE (one reply so far): one model invocation comparing the
  options and naming a winner, also a think step.
- Phase EXECUTE: follow the picked approach (tools ride along). A reply
  containing the done marker ends the run (`FinishStep`); tool calls are
  handed back; plain text is a think step.

Phase advancement is by transcript position (assistant-turn count), not
marker detection — live models drift on marker placement, casing, and
wording. The done marker still gates the finish and matches as a
case-insensitive substring. Params via `ctx.metadata["strategy_params"]`
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
    "above for correctness, simplicity, and risk. Do NOT repeat the options. "
    "Reply with ONLY the verdict line, starting with the picked marker, the "
    "winning option letter, and a one-line reason."
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
        # Markers match as case-insensitive substrings, and phase advancement
        # is by transcript position (assistant-turn count) — live models drift
        # on marker placement ("...DONE:" at the end), casing ("picked"), and
        # wording ("✅ B:" for "PICKED: B"); the turn count is model-proof.
        # Markers still drive the instructions and the done check.
        done_marker_l = done_marker.lower()
        last_text = assistant_texts[-1] if assistant_texts else ""
        phase = len(assistant_texts)  # 0 = diverge, 1 = evaluate, >=2 = execute

        if phase == 0:
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

        if phase == 1:
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

        # Phase EXECUTE — a winner was named (turn 2); tools ride along and a
        # DONE reply ends the run.
        if done_marker_l in last_text.lower():
            return FinishStep(assistant_message=messages[-1], finish_reason="stop")

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
        if done_marker_l in response.message.text.lower():
            return FinishStep(assistant_message=response.message, finish_reason="stop")
        if response.message.tool_calls:
            return ToolCallsStep(
                assistant_message=response.message, tool_calls=response.message.tool_calls
            )
        return ToolCallsStep(assistant_message=response.message)  # think step
