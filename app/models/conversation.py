from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class Conversation(Base):
    """A chat thread for a single user, optionally bound to one channel.

    Channel values: 'web' | 'telegram'. A user may have multiple conversations
    (e.g. one per browser session, one per Telegram chat).
    """

    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    channel = Column(String(32), nullable=False, index=True)
    # Optional binding: if a conversation is "with" a specific agent, store its id.
    agent_id = Column(Integer, nullable=True, index=True)
    # Optional channel-side identifier (e.g. Telegram chat_id) so we can resolve
    # an existing conversation for an inbound message without extra joins.
    external_ref = Column(String(128), nullable=True, index=True)
    title = Column(String(200), nullable=True)
    # JSON-encoded blob for arbitrary metadata (kept as Text for portability).
    meta_json = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_message_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
