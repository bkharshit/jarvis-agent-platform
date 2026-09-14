"""Conversation memory for one segment (S12, ADR 0016 §2).

Loads the history view a prompt needs — the full post-boundary history the
PromptEngine windows, plus the rolling summary when the summarize
strategy is on — and compacts a conversation that outgrew the window.
Called by AgentRuntime inside the segment's try (the D28 site), so the
never-raise rules apply at the caller: cancellation propagates, a
summarizer ModelError degrades the segment to the plain window (D45 —
memory can never fail a run).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jarvis.domain.agent import AgentDefinition, ConversationMemoryState
from jarvis.domain.execution import ExecutionCancelled, ExecutionContext
from jarvis.domain.message import Message
from jarvis.models.errors import ModelAbortedError, ModelError
from jarvis.models.types import ModelRequest, ModelResponse
from jarvis.ports.model import ModelClient
from jarvis.ports.repository import ConversationRepo

logger = logging.getLogger("jarvis.memory")

_SUMMARY_SYSTEM_PROMPT = (
    "You compress conversation history for a long-running assistant. Write a "
    "compact, factual summary in the same language as the conversation. "
    "Preserve names, numbers, decisions, and open questions. Reply with the "
    "summary only — no preamble."
)


@dataclass
class MemoryView:
    """What a segment's prompt build needs from memory (S12, D45)."""

    history: list[Message]  # full post-boundary history; the engine windows
    summary: str | None = None
    conversation_id: str | None = None


class ConversationMemory:
    """The memory transform between ConversationRepo history and the prompt.

    The window strategy is a pure read (exactly the pre-S12 behavior);
    the summarize strategy adds compaction — one `generate()` through the
    run's already-resolved client, usage into `ctx.usage`, state persisted
    through the two S12 ConversationRepo methods.
    """

    def __init__(self, conversations: ConversationRepo) -> None:
        self._conversations = conversations

    async def load(
        self,
        agent: AgentDefinition,
        ctx: ExecutionContext,
        *,
        client: ModelClient | None = None,
    ) -> MemoryView:
        """The history view for this segment. Creates the conversation (as
        the pre-S12 `_load_memory` did) and compacts when the summarize
        strategy is on and the boundary has moved past the window."""
        if not agent.memory.enabled or not ctx.session_id:
            return MemoryView(history=[])
        conversation_id = await self._conversations.get_or_create(
            agent.id, ctx.session_id, tenant_id=ctx.tenant_id
        )
        history = await self._conversations.history(conversation_id)
        state = await self._load_state(conversation_id, ctx)
        if agent.memory.strategy == "summarize" and client is not None:
            state = await self._maybe_compact(agent, ctx, client, conversation_id, history, state)
        return MemoryView(history=history, summary=state.summary, conversation_id=conversation_id)

    async def rebuild_view(
        self,
        agent: AgentDefinition,
        ctx: ExecutionContext,
        conversation_id: str,
    ) -> MemoryView:
        """The read-only view for a RESUMED segment (S12, D45): existing
        summary + windowed history, never a compaction call — a resumed
        segment must not spend tokens before re-entering its paused
        iteration. Fixes the S6-documented v1 approximation where the
        rebuild fed the FULL conversation."""
        if not agent.memory.enabled:
            # Defensive (a conversation exists only for memory-enabled runs,
            # and resume pins the version that created it): memory off means
            # the run transcript is the context — the conversation contributes
            # nothing.
            return MemoryView(history=[], conversation_id=conversation_id)
        history = await self._conversations.history(conversation_id)
        state = await self._load_state(conversation_id, ctx)
        history = history[-agent.memory.max_messages :]  # max_messages >= 1
        return MemoryView(history=history, summary=state.summary, conversation_id=conversation_id)

    # --- compaction (D45) -----------------------------------------------------

    async def _maybe_compact(
        self,
        agent: AgentDefinition,
        ctx: ExecutionContext,
        client: ModelClient,
        conversation_id: str,
        history: list[Message],
        state: ConversationMemoryState,
    ) -> ConversationMemoryState:
        keep = agent.memory.max_messages
        evictable = len(history) - state.summarized_count - keep
        if evictable <= 0:
            return state
        slice_ = history[state.summarized_count : len(history) - keep]
        try:
            response = await self._summarize(client, state.summary, slice_)
        except (ExecutionCancelled, ModelAbortedError):
            raise  # cancellation wins (D45) — the runtime's terminal handlers own it
        except ModelError as exc:
            # Degrade: this segment runs with the plain window and whatever
            # summary state already exists. Memory never fails a run.
            logger.warning(
                "summary compaction failed for conversation %s (%s: %s) — degrading to window",
                conversation_id,
                type(exc).__name__,
                exc,
            )
            return state
        # The compaction call is a model call like any other — its usage
        # rides ctx.usage so the orchestrator's budget spans it (ADR 0004).
        ctx.usage = ctx.usage.plus(response.usage)
        summary = response.message.text
        new_count = state.summarized_count + len(slice_)
        await self._conversations.save_summary(
            conversation_id,
            summary=summary,
            summarized_count=new_count,
            tenant_id=ctx.tenant_id,
        )
        logger.info(
            "compacted conversation %s: %d messages summarized into a rolling summary",
            conversation_id,
            len(slice_),
        )
        return ConversationMemoryState(summary=summary, summarized_count=new_count)

    async def _summarize(
        self, client: ModelClient, previous: str | None, messages: list[Message]
    ) -> ModelResponse:
        """One non-streaming call through the segment's client (no second
        model, no new credential surface — D45)."""
        transcript = "\n".join(f"{m.role}: {m.text}" for m in messages)
        if previous:
            content = f"Previous summary:\n{previous}\n\nAdditional messages:\n{transcript}"
        else:
            content = f"Messages:\n{transcript}"
        request = ModelRequest(
            model=client.ref.model,
            messages=[
                Message(role="system", content=_SUMMARY_SYSTEM_PROMPT),
                Message(role="user", content=content),
            ],
        )
        return await client.generate(request)

    async def _load_state(
        self, conversation_id: str, ctx: ExecutionContext
    ) -> ConversationMemoryState:
        state = await self._conversations.get_summary_state(
            conversation_id, tenant_id=ctx.tenant_id
        )
        return state if state is not None else ConversationMemoryState()


__all__ = ["ConversationMemory", "MemoryView"]
