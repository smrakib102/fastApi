import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_user, require_user
from app.core.config import settings
from app.core.crypto import decrypt_value, encrypt_value
from app.models.admin_setting import AdminSetting
from app.models.telegram_link import TelegramLink
from app.models.tool_credential import ToolCredential
from app.models.tool_registry import ToolRegistry
from app.models.tool_request import ToolRequest
from app.models.user import User
from app.services.audit_log import record_audit

router = APIRouter()


def _get_bot_token(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "telegram_bot_token")
    ).scalar_one_or_none()
    return decrypt_value(setting.value) if setting and setting.value else settings.telegram_bot_token


def _send_telegram_message(
    db: Session,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> None:
    bot_token = _get_bot_token(db)
    if not bot_token:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    httpx.post(url, json=payload, timeout=20)


@router.get("")
def list_tools(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    tools = db.execute(
        select(ToolRegistry).where(
            or_(ToolRegistry.is_global.is_(True), ToolRegistry.user_id == current_user.id)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": tool.id,
                "name": tool.name,
                "category": tool.category,
                "provider": tool.provider,
                "is_global": tool.is_global,
                "auth_type": tool.auth_type,
                "required_fields": json.loads(tool.required_fields or "[]"),
                "description": tool.description,
            }
            for tool in tools
        ]
    }


@router.post("")
def create_global_tool(
    name: str = Form(...),
    category: str | None = Form(default=None),
    provider: str | None = Form(default=None),
    auth_type: str | None = Form(default=None),
    required_fields: str | None = Form(default=None),
    description: str | None = Form(default=None),
    endpoint: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    tool = ToolRegistry(
        name=name,
        category=category,
        provider=provider,
        is_global=True,
        auth_type=auth_type,
        required_fields=required_fields,
        description=description,
        endpoint=endpoint,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return {"id": tool.id}


@router.post("/user")
def create_user_tool(
    name: str = Form(...),
    category: str | None = Form(default=None),
    provider: str | None = Form(default=None),
    auth_type: str | None = Form(default=None),
    required_fields: str | None = Form(default=None),
    description: str | None = Form(default=None),
    endpoint: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    tool = ToolRegistry(
        name=name,
        category=category,
        provider=provider,
        is_global=False,
        user_id=current_user.id,
        auth_type=auth_type,
        required_fields=required_fields,
        description=description,
        endpoint=endpoint,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return {"id": tool.id}


@router.post("/request")
def request_tool_access(
    tool_name: str = Form(...),
    details: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    request = ToolRequest(user_id=current_user.id, tool_name=tool_name, details=details)
    db.add(request)
    db.commit()
    db.refresh(request)

    link = db.execute(
        select(TelegramLink).where(TelegramLink.user_id == current_user.id)
    ).scalar_one_or_none()
    if link:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Connect OAuth", "callback_data": f"toolreq:oauth:{request.id}"},
                    {"text": "Add API key", "callback_data": f"toolreq:apikey:{request.id}"},
                ],
                [
                    {"text": "Skip", "callback_data": f"toolreq:skip:{request.id}"},
                ],
            ]
        }
        details_text = f"\n<pre>{details}</pre>" if details else ""
        _send_telegram_message(
            db,
            link.telegram_user_id,
            "<b>Tool access needed</b>\n"
            f"Tool: <code>{tool_name}</code>{details_text}\n\n"
            "Choose one option below:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    return {"ok": True, "request_id": request.id}


@router.post("/setkey")
def set_tool_key(
    tool_name: str = Form(...),
    api_key: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    tool = db.execute(
        select(ToolRegistry).where(
            ToolRegistry.name == tool_name,
            or_(ToolRegistry.is_global.is_(True), ToolRegistry.user_id == current_user.id),
        )
    ).scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    db.add(ToolCredential(user_id=current_user.id, tool_id=tool.id, secret=encrypt_value(api_key)))
    record_audit(
        db,
        current_user.id,
        "set_tool_key",
        "tool_credential",
        str(tool.id),
        {"tool_name": tool.name},
    )

    req = db.execute(
        select(ToolRequest)
        .where(ToolRequest.user_id == current_user.id, ToolRequest.tool_name == tool_name)
        .order_by(ToolRequest.created_at.desc())
    ).scalar_one_or_none()
    if req:
        req.status = "resolved"
        req.resolved_at = datetime.now(timezone.utc)

    db.commit()
    return {"ok": True}
