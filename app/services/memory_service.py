"""MemoryService — unified cross-channel chat memory.

Phase 1 of the unified chat refactor. Owns reads/writes for the new
`conversations` and `chat_messages` tables. Nothing in the existing codebase
calls into this service yet; it will be wired up by ChatService in Phase 2.

Design notes:
- Channel-agnostic: callers pass channel='web' | 'telegram'.
- Idempotent conversation resolution via (user_id, channel, external_ref).
- All metadata stored as JSON-encoded text (Postgres JSONB upgrade can come
  later without changing the API surface).
- No business logic here — only persistence + retrieval primitives.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat_message import ChatMessage
from app.models.conversation import Conversation


# ---- channel constants -----------------------------------------------------
CHANNEL_WEB = "web"
CHANNEL_TELEGRAM = "telegram"

# ---- role constants --------------------------------------------------------
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
ROLE_TOOL = "tool"


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _loads(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class MemoryService:
    """Persistence layer for unified chat memory.

    Stateless: every method receives a SQLAlchemy Session. Caller is
    responsible for commit/rollback so we can compose with existing
    request-scoped sessions.
    """

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    @staticmethod
    def get_or_create_conversation(
        db: Session,
        *,
        user_id: int,
        channel: str,
        external_ref: Optional[str] = None,
        agent_id: Optional[int] = None,
        title: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Conversation:
        """Resolve a conversation by (user_id, channel, external_ref).

        If `external_ref` is None, returns the most recent conversation for
        the user+channel pair, or creates a new one if none exists.
        """
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .where(Conversation.channel == channel)
        )
        if external_ref is not None:
            stmt = stmt.where(Conversation.external_ref == external_ref)
        else:
            stmt = stmt.where(Conversation.external_ref.is_(None))
        stmt = stmt.order_by(Conversation.last_message_at.desc()).limit(1)

        existing = db.execute(stmt).scalar_one_or_none()
        if existing is not None:
            # Opportunistically update agent_id / title if caller passed new ones
            # and the existing record didn't have them. Keeps schema clean.
            dirty = False
            if agent_id is not None and existing.agent_id != agent_id:
                existing.agent_id = agent_id
                dirty = True
            if title and not existing.title:
                existing.title = title
                dirty = True
            if dirty:
                db.flush()
            return existing

        conv = Conversation(
            user_id=user_id,
            channel=channel,
            external_ref=external_ref,
            agent_id=agent_id,
            title=title,
            meta_json=_dumps(metadata),
        )
        db.add(conv)
        db.flush()
        return conv

    @staticmethod
    def get_conversation(db: Session, conversation_id: int) -> Optional[Conversation]:
        return db.get(Conversation, conversation_id)

    @staticmethod
    def list_user_conversations(
        db: Session, *, user_id: int, channel: Optional[str] = None, limit: int = 20
    ) -> list[Conversation]:
        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if channel:
            stmt = stmt.where(Conversation.channel == channel)
        stmt = stmt.order_by(Conversation.last_message_at.desc()).limit(limit)
        return list(db.execute(stmt).scalars().all())

    @staticmethod
    def bind_agent(db: Session, conversation: Conversation, agent_id: int) -> None:
        if conversation.agent_id != agent_id:
            conversation.agent_id = agent_id
            db.flush()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    @staticmethod
    def append_message(
        db: Session,
        *,
        conversation: Conversation,
        role: str,
        content: Optional[str],
        intent: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> ChatMessage:
        msg = ChatMessage(
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            role=role,
            content=content,
            intent=intent,
            meta_json=_dumps(metadata),
        )
        db.add(msg)
        # Bump last_message_at so list ordering stays correct.
        conversation.last_message_at = datetime.now(timezone.utc)
        db.flush()
        return msg

    @staticmethod
    def recent_messages(
        db: Session, *, conversation_id: int, limit: int = 20
    ) -> list[ChatMessage]:
        """Return the most recent `limit` messages in chronological order."""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        rows = list(db.execute(stmt).scalars().all())
        rows.reverse()
        return rows

    @staticmethod
    def render_for_llm(messages: Iterable[ChatMessage]) -> list[dict]:
        """Convert ChatMessage rows into the {role, content} format used by
        LLM chat APIs. `tool` messages are flattened into assistant context.
        """
        out: list[dict] = []
        for m in messages:
            role = m.role if m.role in {ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM} else ROLE_ASSISTANT
            out.append({"role": role, "content": m.content or ""})
        return out

    @staticmethod
    def message_metadata(message: ChatMessage) -> Any:
        return _loads(message.meta_json)


memory_service = MemoryService()
