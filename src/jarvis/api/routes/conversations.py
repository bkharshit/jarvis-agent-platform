"""Conversations: per-(agent, session) message history (plan §5)."""

from __future__ import annotations

from fastapi import APIRouter

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.errors import ApiError
from jarvis.api.schemas import MessageList

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("/{agent_id}/{session_id}/messages")
async def conversation_messages(
    agent_id: str,
    session_id: str,
    limit: int | None = None,
    auth: AuthContext = AuthDep,
) -> MessageList:
    conversation_id = await auth.conversations.find(agent_id, session_id)
    if conversation_id is None:
        raise ApiError(
            404, "not_found", f"no conversation for agent {agent_id!r} / session {session_id!r}"
        )
    messages = await auth.conversations.history(conversation_id, limit)
    return MessageList(agent_id=agent_id, session_id=session_id, messages=messages)


__all__ = ["router"]
