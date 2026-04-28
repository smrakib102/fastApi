import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_value
from app.db.session import SessionLocal
from app.models.admin_setting import AdminSetting

logger = logging.getLogger(__name__)


def _get_setting(db: Session, key: str) -> str | None:
    setting = db.execute(select(AdminSetting).where(AdminSetting.key == key)).scalar_one_or_none()
    return setting.value if setting and setting.value else None


def _resolve_bot_token(db: Session) -> tuple[str | None, str]:
    if settings.secrets_env_only:
        return settings.telegram_bot_token, "env"

    token = _get_setting(db, "telegram_bot_token")
    if token:
        return decrypt_value(token), "db"

    if settings.telegram_bot_token:
        return settings.telegram_bot_token, "env"

    return None, "missing"


def _resolve_bot_username(db: Session) -> tuple[str | None, str]:
    value = _get_setting(db, "telegram_bot_username")
    if value:
        return value, "db"
    if settings.telegram_bot_username:
        return settings.telegram_bot_username, "env"
    return None, "missing"


def _fetch_bot_status(bot_token: str) -> dict[str, Any]:
    try:
        me_resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=10,
        )
        webhook_resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"telegram_request_failed: {exc}"}

    try:
        me_json = me_resp.json()
    except Exception:  # noqa: BLE001
        me_json = {"ok": False, "description": me_resp.text[:200]}

    try:
        webhook_json = webhook_resp.json()
    except Exception:  # noqa: BLE001
        webhook_json = {"ok": False, "description": webhook_resp.text[:200]}

    if not me_json.get("ok"):
        return {"ok": False, "error": me_json.get("description") or "telegram_getMe_failed"}

    return {
        "ok": True,
        "bot": me_json.get("result", {}),
        "webhook": webhook_json.get("result", {}) if webhook_json.get("ok") else {},
    }


def get_telegram_status(db: Session) -> dict[str, Any]:
    bot_token, token_source = _resolve_bot_token(db)
    configured_username, username_source = _resolve_bot_username(db)

    if not bot_token:
        return {
            "ok": False,
            "environment": settings.environment,
            "token_source": token_source,
            "configured_username": configured_username,
            "configured_username_source": username_source,
            "error": "telegram_bot_token_missing",
        }

    status = _fetch_bot_status(bot_token)
    payload: dict[str, Any] = {
        "ok": status.get("ok", False),
        "environment": settings.environment,
        "token_source": token_source,
        "configured_username": configured_username,
        "configured_username_source": username_source,
    }

    if not status.get("ok"):
        payload["error"] = status.get("error")
        return payload

    bot = status.get("bot", {})
    webhook = status.get("webhook", {})
    payload.update(
        {
            "bot_username": bot.get("username"),
            "bot_id": bot.get("id"),
            "webhook_url": webhook.get("url"),
            "webhook_has_custom_cert": webhook.get("has_custom_certificate"),
            "webhook_pending_updates": webhook.get("pending_update_count"),
            "webhook_last_error_date": webhook.get("last_error_date"),
            "webhook_last_error_message": webhook.get("last_error_message"),
        }
    )
    return payload


def log_telegram_status_on_startup() -> None:
    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_status_db_unavailable", extra={"error": str(exc)})
        return

    try:
        status = get_telegram_status(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_status_failed", extra={"error": str(exc)})
        return
    finally:
        db.close()

    if status.get("ok"):
        logger.info(
            "telegram_status",
            extra={
                "environment": status.get("environment"),
                "token_source": status.get("token_source"),
                "configured_username": status.get("configured_username"),
                "bot_username": status.get("bot_username"),
                "webhook_url": status.get("webhook_url"),
                "webhook_pending_updates": status.get("webhook_pending_updates"),
            },
        )
    else:
        logger.warning(
            "telegram_status",
            extra={
                "environment": status.get("environment"),
                "token_source": status.get("token_source"),
                "configured_username": status.get("configured_username"),
                "error": status.get("error"),
            },
        )
