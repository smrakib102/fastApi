from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    chat_id = Column(String(64), nullable=False)
    chat_type = Column(String(32), nullable=False)
    message_id = Column(String(64), nullable=False)
    sender_id = Column(String(64), nullable=True)
    sender_name = Column(String(200), nullable=True)
    text = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    raw_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
