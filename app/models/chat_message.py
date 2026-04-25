from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ChatMessage(Base):
    """A single turn in a conversation.

    role: 'user' | 'assistant' | 'system' | 'tool'
    intent: optional classifier label (e.g. 'create_agent', 'run_agent', 'general_chat').
    meta_json: arbitrary JSON-encoded metadata (tool name, agent_run_id, permission
               request payload, etc.).
    """

    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=True)
    intent = Column(String(64), nullable=True)
    meta_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
