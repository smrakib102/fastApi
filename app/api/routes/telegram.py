import json
import secrets

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_legacy_user, require_user
from app.core.config import settings
from app.core.redis_client import get_redis
from app.models.employee import Employee
from app.models.admin_setting import AdminSetting
from app.models.telegram_link import TelegramLink
from app.models.tool_request import ToolRequest
from app.models.tool_registry import ToolRegistry
from app.models.tool_credential import ToolCredential
from app.models.user import User

router = APIRouter()


def _extract_message(update: dict) -> dict | None:
    return update.get("message") or update.get("edited_message")


def _extract_callback(update: dict) -> dict | None:
    return update.get("callback_query")


def _build_display_name(message: dict) -> str:
    from_user = message.get("from", {})
    parts = [from_user.get("first_name"), from_user.get("last_name")]
    name = " ".join([p for p in parts if p])
    if name:
        return name
    return from_user.get("username") or "telegram-user"


def _extract_start_token(text: str | None) -> str | None:
    if not text:
        return None
    if not text.startswith("/start"):
        return None
    parts = text.split(" ", 1)
    if len(parts) < 2:
        return None
    return parts[1].strip()


def _extract_setkey(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    if not text.startswith("/setkey "):
        return None
    parts = text.split(" ", 2)
    if len(parts) < 3:
        return None
    return parts[1].strip(), parts[2].strip()


def _get_bot_username(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "telegram_bot_username")
    ).scalar_one_or_none()
    return setting.value if setting and setting.value else settings.telegram_bot_username


def _get_bot_token(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "telegram_bot_token")
    ).scalar_one_or_none()
    return setting.value if setting and setting.value else settings.telegram_bot_token


def _send_message(db: Session, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
    bot_token = _get_bot_token(db)
    if not bot_token:
        return
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    httpx.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload,
        timeout=20,
    )


def _answer_callback(db: Session, callback_id: str, text: str) -> None:
    bot_token = _get_bot_token(db)
    if not bot_token:
        return
    httpx.post(
        f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text},
        timeout=20,
    )


@router.post("/link-token")
def create_link_token(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    token = secrets.token_urlsafe(16)
    redis_client = get_redis()
    redis_client.setex(
        f"telegram:link:{token}",
        settings.telegram_link_ttl_seconds,
        str(current_user.id),
    )

    link = None
    bot_username = _get_bot_username(db)
    if bot_username:
        link = f"https://t.me/{bot_username}?start={token}"

    return {"token": token, "link": link, "expires_in": settings.telegram_link_ttl_seconds}


@router.post("/webhook")
def telegram_webhook(
    update: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if settings.telegram_webhook_secret:
        if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="Unauthorized")

    callback = _extract_callback(update)
    message = _extract_message(update)
    if not message and not callback:
        return {"ok": True}

    if callback:
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        from_user = callback.get("from", {})
        chat = callback.get("message", {}).get("chat", {})
        chat_id = chat.get("id")
        if not chat_id:
            return {"ok": True}

        parts = data.split(":", 2)
        if len(parts) < 3 or parts[0] != "toolreq":
            if callback_id:
                _answer_callback(db, callback_id, "Unsupported action")
            return {"ok": True}

        action = parts[1]
        request_id = parts[2]
        link = db.execute(
            select(TelegramLink).where(TelegramLink.telegram_user_id == str(chat_id))
        ).scalar_one_or_none()
        user_id = link.user_id if link else None
        if not user_id:
            if callback_id:
                _answer_callback(db, callback_id, "Please link your account first")
            return {"ok": True}

        req = db.execute(
            select(ToolRequest).where(ToolRequest.id == int(request_id))
        ).scalar_one_or_none()
        if not req:
            if callback_id:
                _answer_callback(db, callback_id, "Request not found")
            return {"ok": True}

        if action == "apikey":
            redis_client = get_redis()
            redis_client.setex(
                f"telegram:pending:{chat_id}",
                settings.telegram_prompt_ttl_seconds,
                json.dumps(
                    {
                        "action": "setkey",
                        "user_id": user_id,
                        "tool_name": req.tool_name,
                        "request_id": req.id,
                    }
                ),
            )
            _send_message(
                db,
                chat_id,
                "<b>Almost there</b>\n"
                f"Please paste the API key for <code>{req.tool_name}</code>.",
            )
            if callback_id:
                _answer_callback(db, callback_id, "Send the API key")
            return {"ok": True}

        if action == "skip":
            req.status = "skipped"
            db.add(req)
            db.commit()
            _send_message(
                db,
                chat_id,
                f"Skipped <code>{req.tool_name}</code>. You can add it later.",
            )
            if callback_id:
                _answer_callback(db, callback_id, "Skipped")
            return {"ok": True}

        if action == "oauth":
            req.status = "waiting_oauth"
            db.add(req)
            db.commit()
            _send_message(
                db,
                chat_id,
                "OAuth flow is not configured yet. Please choose API key for now.",
            )
            if callback_id:
                _answer_callback(db, callback_id, "OAuth not ready")
            return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    name = _build_display_name(message)
    text = message.get("text")
    start_token = _extract_start_token(text)
    setkey = _extract_setkey(text)

    linked_user_id: int | None = None
    if start_token:
        redis_client = get_redis()
        user_id_value = redis_client.get(f"telegram:link:{start_token}")
        if user_id_value:
            linked_user_id = int(user_id_value)
            redis_client.delete(f"telegram:link:{start_token}")

            existing_link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == str(chat_id))
            ).scalar_one_or_none()
            if existing_link:
                existing_link.user_id = linked_user_id
                existing_link.display_name = name
            else:
                db.add(
                    TelegramLink(
                        user_id=linked_user_id,
                        telegram_user_id=str(chat_id),
                        display_name=name,
                    )
                )

    existing = db.execute(
        select(Employee).where(Employee.telegram_chat_id == str(chat_id))
    ).scalar_one_or_none()

    legacy_user = get_legacy_user(db)
    if existing:
        existing.name = name
        if linked_user_id:
            existing.user_id = linked_user_id
        elif not existing.user_id:
            existing.user_id = legacy_user.id
    else:
        db.add(
            Employee(
                name=name,
                telegram_chat_id=str(chat_id),
                user_id=linked_user_id or legacy_user.id,
            )
        )

    db.commit()

    pending_raw = get_redis().get(f"telegram:pending:{chat_id}")
    if pending_raw and text and not text.startswith("/"):
        pending = json.loads(pending_raw)
        if pending.get("action") == "setkey":
            tool_name = pending.get("tool_name")
            api_key = text.strip()
            user_id = int(pending.get("user_id"))
            tool = db.execute(
                select(ToolRegistry).where(ToolRegistry.name == tool_name)
            ).scalar_one_or_none()
            if tool:
                db.add(ToolCredential(user_id=user_id, tool_id=tool.id, secret=api_key))
                req = db.execute(
                    select(ToolRequest)
                    .where(ToolRequest.user_id == user_id, ToolRequest.tool_name == tool_name)
                    .order_by(ToolRequest.created_at.desc())
                ).scalar_one_or_none()
                if req:
                    req.status = "resolved"
                db.commit()

                get_redis().delete(f"telegram:pending:{chat_id}")
                _send_message(
                    db,
                    chat_id,
                    f"Saved key for <code>{tool_name}</code>. You can continue now.",
                )
            return {"ok": True}

    if setkey:
        tool_name, api_key = setkey
        user_id = linked_user_id or (existing.user_id if existing else legacy_user.id)
        tool = db.execute(
            select(ToolRegistry).where(ToolRegistry.name == tool_name)
        ).scalar_one_or_none()
        if tool:
            db.add(ToolCredential(user_id=user_id, tool_id=tool.id, secret=api_key))
            req = db.execute(
                select(ToolRequest)
                .where(ToolRequest.user_id == user_id, ToolRequest.tool_name == tool_name)
                .order_by(ToolRequest.created_at.desc())
            ).scalar_one_or_none()
            if req:
                req.status = "resolved"
            db.commit()

            _send_message(
                db,
                chat_id,
                f"Saved key for <code>{tool_name}</code>. You can continue now.",
            )

    return {"ok": True}
