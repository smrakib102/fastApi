from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_value
from app.models.telegram_bot import TelegramBot


def get_user_bot(db: Session, user_id: int, *, active_only: bool = True) -> TelegramBot | None:
    stmt = select(TelegramBot).where(TelegramBot.user_id == user_id)
    bot = db.execute(stmt).scalar_one_or_none()
    if not bot:
        return None
    if active_only and bot.status != "active":
        return None
    return bot


def get_user_bot_token(db: Session, user_id: int) -> str | None:
    bot = get_user_bot(db, user_id, active_only=True)
    if not bot or not bot.bot_token:
        return None
    try:
        return decrypt_value(bot.bot_token)
    except Exception:  # noqa: BLE001
        return None


def get_user_bot_username(db: Session, user_id: int) -> str | None:
    bot = get_user_bot(db, user_id, active_only=True)
    return bot.bot_username if bot else None


def get_bot_by_webhook_secret(db: Session, secret: str) -> TelegramBot | None:
    stmt = select(TelegramBot).where(
        TelegramBot.webhook_secret == secret,
        TelegramBot.status == "active",
    )
    return db.execute(stmt).scalar_one_or_none()
