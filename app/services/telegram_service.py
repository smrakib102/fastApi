import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_value
from app.models.admin_setting import AdminSetting


def _get_bot_token(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "telegram_bot_token")
    ).scalar_one_or_none()
    # Phase 7: when secrets_env_only is on, only env-provided value is used.
    if settings.secrets_env_only:
        return settings.telegram_bot_token
    return decrypt_value(setting.value) if setting and setting.value else settings.telegram_bot_token


def send_message(db: Session, chat_id: str, text: str) -> dict | None:
    """Send a Telegram message. Returns the parsed Bot API response dict
    (so callers can detect ``ok: false`` cases like "bot was kicked")
    or None if no token is configured / the request couldn't be sent.
    """
    bot_token = _get_bot_token(db)
    if not bot_token:
        return None
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=20,
        )
    except Exception:  # noqa: BLE001 — caller decides what to do on failure
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "description": resp.text[:200]}
