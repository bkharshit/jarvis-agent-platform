"""The S14 handoff tool (ADR 0018 §6, D56) — `handoff__<agent>` bindings.

A handoff is the model's control-flow decision mid-run: the tool enqueues a
SUCCESSOR run (ordinary queue run, shared trace + session, the pinned target
version), transfers the predecessor's transcript as rendered context (plus
the model's note), and returns immediately — it never blocks on the
successor. The RUNTIME ends the predecessor: the loop sees the tool's
`kind: "handoff"` metadata after the batch and maps the run to the ordinary
`run.completed` terminal (no new status, no new event type).
"""

from __future__ import annotations

from typing import Any, Protocol

from jarvis.domain.agent import AgentVersion
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.runtime.child_runs import (
    QueuedRunWriter,
    child_message,
    enqueue_child_run,
    with_trigger_provenance,
)
from jarvis.tools.base import BaseTool


class TranscriptReader(Protocol):
    """Structural view of the read side the transfer renders from —
    `SqlExecutionRepo.list_messages` (per-run transcript)."""

    async def list_messages(self, run_id: str) -> list[Message]: ...


def render_transcript(messages: list[Message], *, max_chars: int) -> str:
    """Render a run's transcript as labeled text for the successor, truncated
    from the START (the oldest messages drop first — the recent context is
    what a handoff needs). An empty transcript renders empty; the note rides
    separately."""
    rendered = []
    for message in messages:
        label: str = message.role
        if message.role == "tool" and message.name:
            label = f"tool {message.name}"
        rendered.append(f"[{label}] {message.text}")
    kept = list(rendered)
    while len(kept) > 1 and len("\n".join(kept)) > max_chars:
        kept.pop(0)
    text = "\n".join(kept)
    if len(kept) < len(rendered):
        text = "[earlier transcript truncated]\n" + text
    return text


class HandoffTool(BaseTool):
    """One bound handoff target as an ordinary BaseTool. Fire-and-forget:
    enqueue the successor, transfer the rendered transcript + note, return
    "Handed off to <name> (run <id>)". The loop terminates the predecessor
    at the end of the batch (D56)."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        *,
        agent_id: str,
        agent_name: str,
        version_id: str,
        parent_ctx: ExecutionContext,
        executions: QueuedRunWriter,
        transcripts: TranscriptReader,
        max_transcript_chars: int,
        parent_depth: int,
    ) -> None:
        super().__init__(descriptor)
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._version_id = version_id
        self._parent_ctx = parent_ctx
        self._executions = executions
        self._transcripts = transcripts
        self._max_transcript_chars = max_transcript_chars
        self._parent_depth = parent_depth

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        note = arguments.get("note")
        note_text = str(note) if note else ""
        transcript = await self._transcripts.list_messages(self._parent_ctx.run_id)
        rendered = render_transcript(transcript, max_chars=self._max_transcript_chars)
        successor_input = rendered
        if note_text:
            successor_input = (
                f"{rendered}\n\n[handoff note] {note_text}"
                if rendered
                else f"[handoff note] {note_text}"
            )
        # D56: the successor shares the trace AND the session (a handoff
        # chain groups under /executions?session_id=), inherits the parent's
        # absolute deadline, and carries the depth forward so a handoff
        # chain cannot launder the sub-agent depth cap.
        message = child_message(
            agent_id=self._agent_id,
            agent_version_id=self._version_id,
            input=successor_input,
            tenant_id=self._parent_ctx.tenant_id,
            principal=self._parent_ctx.principal,
            session_id=self._parent_ctx.session_id,
            user_id=self._parent_ctx.user_id,
            trace_id=self._parent_ctx.trace_id,
            deadline=self._parent_ctx.deadline,
            parent_run_id=self._parent_ctx.run_id,
            metadata=with_trigger_provenance(
                {
                    "handoff": {"note": note_text or None},
                    "sub_agent": {"depth": self._parent_depth},
                },
                self._parent_ctx.metadata,
            ),
        )
        successor_run_id = await enqueue_child_run(executions=self._executions, message=message)
        # The tool→runtime reporting channel (D54): the loop reads these back
        # after the batch and terminates the predecessor.
        context.result_metadata.update(
            {
                "kind": "handoff",
                "successor_run_id": successor_run_id,
                "agent_id": self._agent_id,
                "agent_name": self._agent_name,
            }
        )
        return f"Handed off to {self._agent_name} (run {successor_run_id})"


def mint_handoff_descriptor(name: str, version: AgentVersion) -> ToolDescriptor:
    snapshot = version.snapshot
    description = (
        f"Hand off this conversation to sub-agent {snapshot.name!r}."
        + (f" {snapshot.description}" if snapshot.description else "")
        + " Your run ENDS and the target agent continues from the transcript;"
        " optionally add a short note about what to do next."
    )
    return ToolDescriptor(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": (
                        "Optional note to the receiving agent: what to do next "
                        "and anything it cannot see in the transcript"
                    ),
                }
            },
            "required": [],
        },
        # D55/§6: a handoff never gates on a human — the transfer is the
        # model's own control-flow decision.
        annotations={"requires_approval": False},
    )


__all__ = ["HandoffTool", "TranscriptReader", "mint_handoff_descriptor", "render_transcript"]
