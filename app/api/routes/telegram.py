import secrets

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

    message = _extract_message(update)
    if not message:
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

    return {"ok": True}
