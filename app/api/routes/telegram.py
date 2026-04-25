import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.core.config import settings
from app.core.redis_client import get_redis
from app.models.employee import Employee
from app.core.crypto import decrypt_value, encrypt_value
from app.models.admin_setting import AdminSetting
from app.models.telegram_link import TelegramLink
from app.models.tool_request import ToolRequest
from app.models.tool_registry import ToolRegistry
from app.models.tool_credential import ToolCredential
from app.models.user import User
from app.models.telegram_message import TelegramMessage
from app.models.agent_template import AgentTemplate
from app.services.agent_runtime import AgentRuntimeError, execute_agent_run
from app.models.agent import Agent
from app.services.audit_log import record_audit

router = APIRouter()

logger = logging.getLogger(__name__)

_rate_window_seconds = 10
_rate_limit_max = 5
_max_text_length = 1200


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


def _extract_run(text: str | None) -> tuple[str, str | None] | None:
    if not text:
        return None
    if not text.startswith("/run "):
        return None
    parts = text.split(" ", 2)
    if len(parts) < 2:
        return None
    agent_ref = parts[1].strip()
    if len(agent_ref) > 80 or not re.fullmatch(r"[a-zA-Z0-9._-]+", agent_ref):
        return None
    prompt = parts[2].strip() if len(parts) > 2 else None
    return agent_ref, prompt


def _enforce_rate_limit(chat_id: str, telegram_user_id: str) -> None:
    redis_client = get_redis()
    chat_key = f"telegram:rate:chat:{chat_id}"
    user_key = f"telegram:rate:user:{telegram_user_id}"

    chat_count = redis_client.incr(chat_key)
    user_count = redis_client.incr(user_key)

    if chat_count == 1:
        redis_client.expire(chat_key, _rate_window_seconds)
    if user_count == 1:
        redis_client.expire(user_key, _rate_window_seconds)

    if chat_count > _rate_limit_max or user_count > _rate_limit_max:
        logger.warning("telegram_rate_limited", extra={"chat_id": chat_id, "telegram_user_id": telegram_user_id})
        raise HTTPException(status_code=429, detail="Rate limited")


def _validate_text(text: str | None) -> None:
    if text and len(text) > _max_text_length:
        logger.warning("telegram_message_rejected", extra={"reason": "message_too_long"})
        raise HTTPException(status_code=400, detail="Message too long")


def _extract_confirm_token(text: str | None) -> str | None:
    if not text:
        return None
    if not text.upper().startswith("CONFIRM "):
        return None
    return text.split(" ", 1)[1].strip()


def _dedupe_update(update_id: int) -> bool:
    redis_client = get_redis()
    key = f"telegram:update:{update_id}"
    return bool(redis_client.set(key, "1", ex=300, nx=True))


def _is_summary_now(text: str | None) -> bool:
    return bool(text and text.strip() == "/summary_now")


def _is_new_agent(text: str | None) -> bool:
    return bool(text and text.strip() == "/newagent")


def _get_bot_username(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "telegram_bot_username")
    ).scalar_one_or_none()
    return setting.value if setting and setting.value else settings.telegram_bot_username


def _get_bot_token(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "telegram_bot_token")
    ).scalar_one_or_none()
    if settings.secrets_env_only:
        return settings.telegram_bot_token
    return decrypt_value(setting.value) if setting and setting.value else settings.telegram_bot_token


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


def _send_tool_request(db: Session, chat_id: str, request_id: int, tool_name: str) -> None:
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Connect OAuth", "callback_data": f"toolreq:oauth:{request_id}"},
                {"text": "Add API key", "callback_data": f"toolreq:apikey:{request_id}"},
            ],
            [
                {"text": "Skip", "callback_data": f"toolreq:skip:{request_id}"},
            ],
        ]
    }
    _send_message(
        db,
        chat_id,
        f"<b>Tool access needed</b>\nTool: <code>{tool_name}</code>\n\nChoose one option below:",
        reply_markup=keyboard,
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


@router.get("/status")
def telegram_status(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    link = db.execute(
        select(TelegramLink).where(TelegramLink.user_id == current_user.id)
    ).scalar_one_or_none()
    bot_username = _get_bot_username(db)
    return {
        "connected": bool(link),
        "bot_username": bot_username,
        "display_name": link.display_name if link else None,
        "telegram_user_id": link.telegram_user_id if link else None,
    }


@router.post("/admin/set-webhook")
def set_telegram_webhook(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Register this server's /telegram/webhook URL with Telegram so the
    bot starts receiving /start and other commands. Idempotent."""
    bot_token = _get_bot_token(db)
    if not bot_token:
        raise HTTPException(status_code=400, detail="telegram_bot_token not configured")
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=400, detail="telegram_webhook_secret not configured")
    if not settings.public_base_url:
        raise HTTPException(status_code=400, detail="public_base_url not configured")

    webhook_url = settings.public_base_url.rstrip("/") + "/telegram/webhook"
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": settings.telegram_webhook_secret,
                "drop_pending_updates": True,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            timeout=20,
        )
        data = resp.json()
    except Exception as exc:  # pragma: no cover - network failure path
        logger.exception("telegram_set_webhook_failed")
        raise HTTPException(status_code=502, detail=f"telegram_api_error: {exc}")

    if not data.get("ok"):
        logger.error("telegram_set_webhook_rejected", extra={"response": data})
        raise HTTPException(status_code=502, detail=data)

    record_audit(
        db,
        user_id=current_user.id,
        action="telegram.webhook_set",
        resource_type="telegram_bot",
        resource_id=None,
        metadata={"webhook_url": webhook_url},
    )
    return {"ok": True, "webhook_url": webhook_url, "telegram_response": data}


@router.get("/admin/webhook-info")
def telegram_webhook_info(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return Telegram's view of the currently registered webhook (for debugging)."""
    bot_token = _get_bot_token(db)
    if not bot_token:
        raise HTTPException(status_code=400, detail="telegram_bot_token not configured")
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
            timeout=20,
        )
        return resp.json()
    except Exception as exc:  # pragma: no cover - network failure path
        raise HTTPException(status_code=502, detail=f"telegram_api_error: {exc}")


@router.post("/webhook")
def telegram_webhook(
    update: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not settings.telegram_webhook_secret:
        logger.error("telegram_webhook_rejected", extra={"reason": "missing_secret"})
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not x_telegram_bot_api_secret_token:
        logger.warning("telegram_webhook_rejected", extra={"reason": "missing_header"})
        raise HTTPException(status_code=401, detail="Unauthorized")
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        logger.warning("telegram_webhook_rejected", extra={"reason": "invalid_secret"})
        raise HTTPException(status_code=401, detail="Unauthorized")

    update_id = update.get("update_id")
    if isinstance(update_id, int):
        if not _dedupe_update(update_id):
            logger.warning("telegram_duplicate_update", extra={"update_id": update_id})
            return {"ok": True}
    else:
        logger.warning("telegram_update_invalid", extra={"reason": "missing_update_id"})
        return {"ok": True}

    callback = _extract_callback(update)
    message = _extract_message(update)
    if not message and not callback:
        return {"ok": True}

    if callback:
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        from_user = callback.get("from", {})
        telegram_user_id = str(from_user.get("id") or "")
        chat = callback.get("message", {}).get("chat", {})
        chat_id = chat.get("id")
        if not chat_id:
            return {"ok": True}
        if not telegram_user_id:
            logger.warning("telegram_user_missing", extra={"chat_id": str(chat_id)})
            return {"ok": True}

        parts = data.split(":", 2)
        if len(parts) < 3 or parts[0] != "toolreq":
            if callback_id:
                _answer_callback(db, callback_id, "Unsupported action")
            return {"ok": True}

        action = parts[1]
        request_id = parts[2]
        link = db.execute(
            select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
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

        # Patch P3: route every decision through PermissionService so the
        # web and Telegram surfaces share the same state machine. The
        # Telegram-specific UX (Redis pending-key state, OAuth bridge
        # link) is layered on top after the service has updated the
        # ToolRequest row.
        from app.models.user import User as _User  # local import to avoid cycle
        from app.services.permission_service import (
            DECISION_ALLOW,
            DECISION_CONNECT,
            DECISION_SKIP,
            permission_service,
        )

        _decision_map = {
            "apikey": DECISION_ALLOW,
            "skip": DECISION_SKIP,
            "oauth": DECISION_CONNECT,
        }
        decision = _decision_map.get(action)
        if not decision:
            if callback_id:
                _answer_callback(db, callback_id, "Unsupported action")
            return {"ok": True}

        user_obj = db.get(_User, user_id)
        if user_obj is None:
            if callback_id:
                _answer_callback(db, callback_id, "User missing")
            return {"ok": True}

        result = permission_service.resolve(
            db, user=user_obj, request_id=req.id, decision=decision
        )
        db.commit()

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
            _send_message(
                db,
                chat_id,
                f"Skipped <code>{req.tool_name}</code>. You can add it later.",
            )
            if callback_id:
                _answer_callback(db, callback_id, "Skipped")
            return {"ok": True}

        if action == "oauth":
            # PermissionService has already moved the row into
            # ``waiting_oauth``. The web "/google/login" URL it returns
            # is replaced here with a Telegram-friendly bridge link
            # (Phase 8) so the user can complete OAuth from a browser.
            base_url = (settings.public_base_url or "").rstrip("/")
            redirect_uri = settings.google_oauth_redirect_uri or ""
            if not base_url and redirect_uri:
                from urllib.parse import urlparse as _urlparse

                parsed = _urlparse(redirect_uri)
                if parsed.scheme and parsed.netloc:
                    base_url = f"{parsed.scheme}://{parsed.netloc}"

            if not result.get("ok") or not base_url or not settings.google_oauth_client_id:
                _send_message(
                    db,
                    chat_id,
                    result.get("message")
                    or "OAuth bridge isn't configured on the server. "
                    "Please ask an admin to set PUBLIC_BASE_URL.",
                )
                if callback_id:
                    _answer_callback(db, callback_id, "OAuth not ready")
                return {"ok": True}

            bridge_token = secrets.token_urlsafe(24)
            redis_client = get_redis()
            redis_client.setex(
                f"oauth:bridge:{bridge_token}",
                settings.google_oauth_state_ttl_seconds,
                str(user_id),
            )
            bridge_url = f"{base_url}/google/oauth/bridge/{bridge_token}"
            _send_message(
                db,
                chat_id,
                "<b>Connect Google</b>\n"
                f"Open this link in your browser to authorize <code>{req.tool_name}</code>:\n"
                f"{bridge_url}",
            )
            if callback_id:
                _answer_callback(db, callback_id, "Open the link to connect")
            return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    from_user = message.get("from", {})
    telegram_user_id = str(from_user.get("id") or "")
    if not telegram_user_id:
        logger.warning("telegram_user_missing", extra={"chat_id": str(chat_id)})
        return {"ok": True}

    _enforce_rate_limit(str(chat_id), telegram_user_id)

    name = _build_display_name(message)
    text = message.get("text")
    _validate_text(text)
    start_token = _extract_start_token(text)
    setkey = _extract_setkey(text)
    run_request = _extract_run(text)
    summary_now = _is_summary_now(text)
    new_agent = _is_new_agent(text)
    confirm_token = _extract_confirm_token(text)

    # Patch P2: when the unified Telegram pipeline is enabled, suppress the
    # legacy regex-driven command branches (/run, /newagent, /summary_now,
    # confirmation tokens) so the message falls through to ChatService at
    # the end of this handler. /start linking and /setkey block are kept,
    # since they rely on Telegram-specific Redis state. Pending-state
    # message handlers above (template wizard, setkey collection) also
    # short-circuit before this block via their own returns and remain
    # unaffected.
    _telegram_unified = (
        settings.unified_chat_telegram_enabled or settings.unified_chat_enabled
    )
    if _telegram_unified:
        run_request = None
        summary_now = False
        new_agent = False
        confirm_token = None

    linked_user_id: int | None = None
    if start_token:
        redis_client = get_redis()
        user_id_value = redis_client.get(f"telegram:link:{start_token}")
        if user_id_value:
            linked_user_id = int(user_id_value)
            redis_client.delete(f"telegram:link:{start_token}")

            existing_link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            if existing_link:
                existing_link.user_id = linked_user_id
                existing_link.display_name = name
            else:
                db.add(
                    TelegramLink(
                        user_id=linked_user_id,
                        telegram_user_id=telegram_user_id,
                        display_name=name,
                    )
                )

    existing = db.execute(
        select(Employee).where(Employee.telegram_chat_id == str(chat_id))
    ).scalar_one_or_none()
    if existing:
        existing.name = name
        if linked_user_id:
            existing.user_id = linked_user_id
    else:
        db.add(
            Employee(
                name=name,
                telegram_chat_id=str(chat_id),
                user_id=linked_user_id,
            )
        )
    db.commit()

    link = db.execute(
        select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
    ).scalar_one_or_none()
    if link:
        linked_user_id = link.user_id

    if not linked_user_id:
        logger.warning("telegram_unlinked_block", extra={"chat_id": str(chat_id)})
        if start_token:
            _send_message(db, chat_id, "Account linked. You can now use bot commands.")
        else:
            _send_message(db, chat_id, "Please link your account first via the dashboard.")
        return {"ok": True}

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
                db.add(ToolCredential(user_id=user_id, tool_id=tool.id, secret=encrypt_value(api_key)))
                record_audit(
                    db,
                    user_id,
                    "set_tool_key",
                    "tool_credential",
                    str(tool.id),
                    {"tool_name": tool.name, "source": "telegram"},
                )
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
        logger.warning("telegram_command_blocked", extra={"command": "/setkey", "chat_id": str(chat_id)})
        _send_message(db, chat_id, "This command is disabled. Use the dashboard to manage keys.")
        return {"ok": True}

    chat_type = chat.get("type") or "unknown"
    sender = message.get("from") or {}
    sent_at = None
    if message.get("date"):
        sent_at = datetime.fromtimestamp(message.get("date"), tz=timezone.utc)

    # Patch P4: when the unified Telegram pipeline is enabled, ChatService
    # → MemoryService is the canonical store (chat_messages table). Skip
    # the legacy telegram_messages insert in that mode so we don't
    # double-write. The table is preserved as a raw audit trail for the
    # pre-unified path.
    if not _telegram_unified:
        db.add(
            TelegramMessage(
                user_id=linked_user_id,
                chat_id=str(chat_id),
                chat_type=chat_type,
                message_id=str(message.get("message_id") or ""),
                sender_id=str(sender.get("id")) if sender.get("id") else None,
                sender_name=_build_display_name(message),
                text=text,
                sent_at=sent_at,
                raw_json=json.dumps(message, ensure_ascii=True),
            )
        )
        db.commit()

    if run_request:
        agent_ref, prompt = run_request
        user_id = linked_user_id
        agent = None
        if agent_ref.isdigit():
            agent = db.execute(
                select(Agent).where(Agent.id == int(agent_ref), Agent.user_id == user_id)
            ).scalar_one_or_none()
        if not agent:
            agent = db.execute(
                select(Agent).where(Agent.name == agent_ref, Agent.user_id == user_id)
            ).scalar_one_or_none()

        if not agent:
            _send_message(db, chat_id, f"Agent not found: <code>{agent_ref}</code>")
            return {"ok": True}

        try:
            run = execute_agent_run(db, agent, user_id, prompt, source="telegram")
        except AgentRuntimeError as exc:
            _send_message(db, chat_id, f"Run failed: {exc}")
            return {"ok": True}

        _send_message(db, chat_id, run.output_text or "(no output)")
        return {"ok": True}

    if summary_now:
        try:
            from app.services.summary_service import generate_summary

            summary = generate_summary(
                db,
                linked_user_id,
                str(chat_id),
                "UTC",
            )
            _send_message(db, chat_id, summary)
        except Exception as exc:
            _send_message(db, chat_id, f"Summary failed: {exc}")
        return {"ok": True}

    if confirm_token:
        stored = get_redis().get(f"telegram:confirm:{chat_id}:{confirm_token}")
        if not stored:
            logger.warning("telegram_confirm_rejected", extra={"chat_id": str(chat_id)})
            _send_message(db, chat_id, "Confirmation token expired or invalid.")
            return {"ok": True}
        payload = json.loads(stored)
        stored_user_id = int(payload.get("user_id") or 0)
        stored_chat_id = str(payload.get("chat_id") or "")
        stored_action = payload.get("action") or ""
        if stored_user_id != linked_user_id or stored_chat_id != str(chat_id) or stored_action != "newagent":
            logger.warning("telegram_confirm_rejected", extra={"chat_id": str(chat_id), "reason": "user_mismatch"})
            _send_message(db, chat_id, "Confirmation token invalid.")
            return {"ok": True}
        get_redis().delete(f"telegram:confirm:{chat_id}:{confirm_token}")
        new_agent = True

    if new_agent:
        if not confirm_token:
            confirm_value = secrets.token_urlsafe(12)
            confirm_payload = {
                "user_id": linked_user_id,
                "chat_id": str(chat_id),
                "action": "newagent",
            }
            get_redis().setex(
                f"telegram:confirm:{chat_id}:{confirm_value}",
                120,
                json.dumps(confirm_payload),
            )
            _send_message(db, chat_id, f"Reply CONFIRM {confirm_value} to start agent creation.")
            return {"ok": True}
        templates = db.execute(select(AgentTemplate)).scalars().all()
        if not templates:
            _send_message(db, chat_id, "No templates available yet.")
            return {"ok": True}

        template_list = "\n".join(
            [f"{template.id}. {template.name}" for template in templates]
        )
        get_redis().setex(
            f"telegram:template:{chat_id}",
            settings.telegram_prompt_ttl_seconds,
            json.dumps({"stage": "choose"}),
        )
        _send_message(db, chat_id, f"Choose a template by ID:\n{template_list}")
        return {"ok": True}

    pending_template_raw = get_redis().get(f"telegram:template:{chat_id}")
    if pending_template_raw and text and not text.startswith("/"):
        pending = json.loads(pending_template_raw)
        stage = pending.get("stage")
        user_id = linked_user_id

        if stage == "choose":
            template = None
            if text.isdigit():
                template = db.execute(
                    select(AgentTemplate).where(AgentTemplate.id == int(text))
                ).scalar_one_or_none()
            if not template:
                template = db.execute(
                    select(AgentTemplate).where(AgentTemplate.name == text)
                ).scalar_one_or_none()
            if not template:
                _send_message(db, chat_id, "Template not found. Reply with a valid ID.")
                return {"ok": True}

            fields = json.loads(template.fields or "[]")
            pending = {
                "stage": "fields",
                "template_id": template.id,
                "fields": fields,
                "index": 0,
                "values": {},
            }
            get_redis().setex(
                f"telegram:template:{chat_id}",
                settings.telegram_prompt_ttl_seconds,
                json.dumps(pending),
            )
            if fields:
                _send_message(db, chat_id, f"{fields[0].get('label') or fields[0].get('key')}")
            else:
                agent = Agent(
                    user_id=user_id,
                    name=f"{template.name} Agent",
                    role=template.description or template.name,
                    model=template.model,
                    tools=template.tools,
                    category=template.category,
                    status="active",
                    template_id=template.id,
                    config=json.dumps({}, ensure_ascii=True),
                )
                db.add(agent)
                db.commit()
                db.refresh(agent)
                required_tools = json.loads(template.tools or "[]")
                for tool_name in required_tools:
                    tool = db.execute(
                        select(ToolRegistry).where(ToolRegistry.name == tool_name)
                    ).scalar_one_or_none()
                    if not tool:
                        request = ToolRequest(user_id=user_id, tool_name=tool_name)
                        db.add(request)
                        db.commit()
                        db.refresh(request)
                        _send_tool_request(db, chat_id, request.id, tool_name)
                        continue
                    credential = db.execute(
                        select(ToolCredential)
                        .where(ToolCredential.user_id == user_id, ToolCredential.tool_id == tool.id)
                    ).scalar_one_or_none()
                    if not credential:
                        request = ToolRequest(user_id=user_id, tool_name=tool_name)
                        db.add(request)
                        db.commit()
                        db.refresh(request)
                        _send_tool_request(db, chat_id, request.id, tool_name)
                db.commit()
                get_redis().delete(f"telegram:template:{chat_id}")
                _send_message(db, chat_id, f"Created agent <code>{agent.name}</code>.")
            return {"ok": True}

        if stage == "fields":
            fields = pending.get("fields") or []
            index = int(pending.get("index") or 0)
            values = pending.get("values") or {}
            if index < len(fields):
                key = fields[index].get("key")
                if key:
                    values[key] = text.strip()
            index += 1

            if index < len(fields):
                pending.update({"index": index, "values": values})
                get_redis().setex(
                    f"telegram:template:{chat_id}",
                    settings.telegram_prompt_ttl_seconds,
                    json.dumps(pending),
                )
                next_label = fields[index].get("label") or fields[index].get("key")
                _send_message(db, chat_id, next_label)
                return {"ok": True}

            template = db.execute(
                select(AgentTemplate).where(AgentTemplate.id == pending.get("template_id"))
            ).scalar_one_or_none()
            if not template:
                _send_message(db, chat_id, "Template not found.")
                return {"ok": True}

            agent_name = values.get("agent_name") or f"{template.name} Agent"
            agent = Agent(
                user_id=user_id,
                name=agent_name,
                role=template.description or template.name,
                model=template.model,
                tools=template.tools,
                category=template.category,
                status="active",
                template_id=template.id,
                config=json.dumps(values, ensure_ascii=True),
            )
            db.add(agent)
            db.commit()
            db.refresh(agent)

            required_tools = json.loads(template.tools or "[]")
            for tool_name in required_tools:
                tool = db.execute(
                    select(ToolRegistry).where(ToolRegistry.name == tool_name)
                ).scalar_one_or_none()
                if not tool:
                    request = ToolRequest(user_id=user_id, tool_name=tool_name)
                    db.add(request)
                    db.commit()
                    db.refresh(request)
                    _send_tool_request(db, chat_id, request.id, tool_name)
                    continue
                credential = db.execute(
                    select(ToolCredential)
                    .where(ToolCredential.user_id == user_id, ToolCredential.tool_id == tool.id)
                ).scalar_one_or_none()
                if not credential:
                    request = ToolRequest(user_id=user_id, tool_name=tool_name)
                    db.add(request)
                    db.commit()
                    db.refresh(request)
                    _send_tool_request(db, chat_id, request.id, tool_name)

            db.commit()
            get_redis().delete(f"telegram:template:{chat_id}")
            _send_message(db, chat_id, f"Created agent <code>{agent.name}</code>.")
            return {"ok": True}

    # ------------------------------------------------------------------
    # Phase 2d: Unified ChatService fallback for free-text messages.
    # Gated by UNIFIED_CHAT_TELEGRAM_ENABLED so existing slash-command
    # behavior is preserved by default. Patch P2: when the unified flag
    # is on, slash commands like /run and /newagent are also routed
    # through ChatService (the IntentRouter already classifies them).
    # Pending-state messages (setkey, template field collection) still
    # short-circuit before this block via their own returns.
    # ------------------------------------------------------------------
    if _telegram_unified and text:
        try:
            from app.models.user import User as _User  # local import to avoid top-level cycle
            from app.services.chat_service import chat_service as _chat_service
            from app.services.memory_service import CHANNEL_TELEGRAM as _CHANNEL_TG

            user_obj = db.get(_User, linked_user_id)
            if user_obj is not None:
                response = _chat_service.handle_message(
                    db,
                    user=user_obj,
                    text=text,
                    channel=_CHANNEL_TG,
                    external_ref=str(chat_id),
                )
                db.commit()
                _send_message(db, chat_id, response.text or "(no response)")
                # Phase 4: render permission_request action cards as inline
                # keyboards. Reuses the existing toolreq:* callback handler
                # since PermissionService writes to the same ToolRequest table.
                for action in (response.actions or []):
                    if action.get("type") != "permission_request":
                        continue
                    try:
                        _send_tool_request(
                            db,
                            chat_id,
                            int(action.get("request_id")),
                            str(action.get("tool_name") or "tool"),
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "telegram_permission_render_failed",
                            extra={"chat_id": str(chat_id)},
                        )
        except Exception:  # noqa: BLE001 — never break the webhook
            logger.exception(
                "telegram_chat_service_error",
                extra={"chat_id": str(chat_id), "user_id": linked_user_id},
            )
            _send_message(db, chat_id, "Sorry — something went wrong. Please try again.")
        return {"ok": True}

    return {"ok": True}


@router.get("/messages")
def list_messages(
    chat_id: str,
    date: str,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format") from exc

    next_day = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    messages = db.execute(
        select(TelegramMessage)
        .where(
            TelegramMessage.user_id == current_user.id,
            TelegramMessage.chat_id == chat_id,
            TelegramMessage.sent_at >= day,
            TelegramMessage.sent_at < next_day,
        )
        .order_by(TelegramMessage.sent_at.asc())
    ).scalars().all()

    return {
        "items": [
            {
                "id": msg.id,
                "chat_id": msg.chat_id,
                "sender_name": msg.sender_name,
                "text": msg.text,
                "sent_at": msg.sent_at,
            }
            for msg in messages
        ]
    }
