"""Unified chat HTTP API — Phase 2c.

Single entry point used by the web chat UI. Mirrors the surface that
Phase 2d will plug Telegram into. Behind the `unified_chat_web_enabled`
feature flag so it can be exposed gradually without breaking existing
flows.

Endpoints:
  POST /chat/message  → process a single user message, return assistant reply.

The existing per-agent run endpoints (`/agents/{id}/run`, `/runs/...`) are
left untouched so the legacy chat experience keeps working until Phase 9
cutover.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.core.config import settings
from app.models.user import User
from app.services.chat_service import chat_service
from app.services.memory_service import (
    CHANNEL_WEB,
    memory_service,
)
from app.services.permission_service import (
    VALID_DECISIONS,
    permission_service,
)


router = APIRouter()


class ChatMessageIn(BaseModel):
    message: str = Field(..., min_length=0, max_length=8000)
    conversation_id: Optional[int] = None


class ChatMessageOut(BaseModel):
    text: str
    intent: str
    actions: list[dict] = Field(default_factory=list)
    data: dict = Field(default_factory=dict)
    conversation_id: int


class PermissionDecisionIn(BaseModel):
    decision: str = Field(..., min_length=1, max_length=32)


class PermissionDecisionOut(BaseModel):
    ok: bool
    status: Optional[str] = None
    message: str
    oauth_url: Optional[str] = None


class ConversationSummary(BaseModel):
    id: int
    channel: str
    agent_id: Optional[int] = None
    title: Optional[str] = None
    last_message_at: Optional[str] = None


class ChatHistoryMessage(BaseModel):
    id: int
    role: str
    content: Optional[str]
    intent: Optional[str]
    created_at: str


def _flag_enabled() -> None:
    """Guard: refuse traffic until the unified-chat flag is flipped on."""
    if not settings.unified_chat_enabled:
        raise HTTPException(
            status_code=404, detail="Unified chat is not enabled on this deployment."
        )


@router.post("/message", response_model=ChatMessageOut)
def post_message(
    payload: ChatMessageIn,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> ChatMessageOut:
    _flag_enabled()

    external_ref: Optional[str] = None
    if payload.conversation_id is not None:
        # Pin to a specific conversation by checking ownership.
        existing = memory_service.get_conversation(db, payload.conversation_id)
        if not existing or existing.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        external_ref = existing.external_ref

    response = chat_service.handle_message(
        db,
        user=current_user,
        text=payload.message,
        channel=CHANNEL_WEB,
        external_ref=external_ref,
    )
    db.commit()

    return ChatMessageOut(
        text=response.text,
        intent=response.intent,
        actions=response.actions,
        data=response.data,
        conversation_id=response.conversation_id or 0,
    )


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> list[ConversationSummary]:
    _flag_enabled()
    rows = memory_service.list_user_conversations(
        db, user_id=current_user.id, channel=CHANNEL_WEB, limit=20
    )
    return [
        ConversationSummary(
            id=r.id,
            channel=r.channel,
            agent_id=r.agent_id,
            title=r.title,
            last_message_at=r.last_message_at.isoformat() if r.last_message_at else None,
        )
        for r in rows
    ]


@router.get("/conversations/{conversation_id}/messages", response_model=list[ChatHistoryMessage])
def get_history(
    conversation_id: int,
    limit: int = 50,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> list[ChatHistoryMessage]:
    _flag_enabled()
    conv = memory_service.get_conversation(db, conversation_id)
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = memory_service.recent_messages(db, conversation_id=conversation_id, limit=limit)
    return [
        ChatHistoryMessage(
            id=m.id,
            role=m.role,
            content=m.content,
            intent=m.intent,
            created_at=m.created_at.isoformat() if m.created_at else "",
        )
        for m in rows
    ]


@router.post("/permission/{request_id}", response_model=PermissionDecisionOut)
def resolve_permission(
    request_id: int,
    payload: PermissionDecisionIn,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> PermissionDecisionOut:
    _flag_enabled()
    if payload.decision not in VALID_DECISIONS:
        raise HTTPException(status_code=400, detail="Invalid decision")
    result = permission_service.resolve(
        db,
        user=current_user,
        request_id=request_id,
        decision=payload.decision,
    )
    db.commit()
    if not result.get("ok"):
        # Surface as 200 with ok=false so the chat UI can show the message
        # without treating it as a hard error.
        return PermissionDecisionOut(
            ok=False,
            status=result.get("status"),
            message=result.get("message", "Could not resolve permission."),
            oauth_url=result.get("oauth_url"),
        )
    return PermissionDecisionOut(
        ok=True,
        status=result.get("status"),
        message=result.get("message", "Done."),
        oauth_url=result.get("oauth_url"),
    )
