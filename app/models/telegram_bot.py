from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


class TelegramBot(Base):
    __tablename__ = "telegram_bots"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    bot_token = Column(String(512), nullable=True)
    bot_username = Column(String(120), nullable=True)
    bot_id = Column(String(64), nullable=True)
    webhook_secret = Column(String(120), nullable=True)
    start_token = Column(String(120), nullable=True)
    status = Column(String(32), nullable=False, default="active")
    connected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disconnected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", name="telegram_bots_user_unique"),
        UniqueConstraint("webhook_secret", name="telegram_bots_webhook_secret_unique"),
        UniqueConstraint("start_token", name="telegram_bots_start_token_unique"),
    )
