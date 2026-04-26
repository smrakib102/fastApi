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


def _send_chat_action(db: Session, chat_id: str, action: str = "typing") -> None:
    """Tell Telegram to show "... is typing" (or another transient status).

    Telegram displays the action for ~5 seconds or until the next message
    arrives, whichever is first. Best-effort — never raise.
    """
    bot_token = _get_bot_token(db)
    if not bot_token:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=5,
        )
    except Exception:  # noqa: BLE001 — typing indicator is non-essential
        logger.debug("telegram_chat_action_failed", extra={"chat_id": str(chat_id)})


def _edit_message_text(
    db: Session,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Edit a previously-sent inline-keyboard message in place."""
    bot_token = _get_bot_token(db)
    if not bot_token:
        return
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/editMessageText",
            json=payload,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logger.debug("telegram_edit_failed", extra={"chat_id": str(chat_id)})


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


# ---- Inline-keyboard helpers for unified ChatService actions --------------

# Telegram caps callback_data at 64 bytes, so we keep payloads tiny:
#   delpick:<agent_id>      → user picked an agent from the picker
#   delconfirm:<agent_id>   → user confirmed the destructive action
#   delcancel               → user cancelled

def _render_agent_picker(
    db: Session,
    chat_id: str,
    prompt: str,
    agents: list[dict],
    action: str = "delete",
) -> None:
    """Render a list of agents as an inline keyboard for the given action."""
    if action not in {"delete", "run"}:
        # Fall back to plain text for unknown actions instead of crashing.
        _send_message(db, chat_id, prompt + "\n(unsupported picker action)")
        return

    icon = "🗑" if action == "delete" else "▶️"
    cb_prefix = "delpick" if action == "delete" else "runpick"

    rows: list[list[dict]] = []
    for a in agents[:25]:  # Telegram inline keyboards: keep it sane
        agent_id = a.get("id")
        name = a.get("name") or f"Agent {agent_id}"
        if agent_id is None:
            continue
        rows.append([{"text": f"{icon} {name}", "callback_data": f"{cb_prefix}:{agent_id}"}])
    rows.append([{"text": "Cancel", "callback_data": "delcancel" if action == "delete" else "runcancel"}])
    _send_message(
        db,
        chat_id,
        f"<b>{prompt}</b>",
        reply_markup={"inline_keyboard": rows},
    )


def _render_delete_confirm(
    db: Session, chat_id: str, agent_id: int, agent_name: str
) -> None:
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Yes, delete", "callback_data": f"delconfirm:{agent_id}"},
                {"text": "❌ Cancel", "callback_data": "delcancel"},
            ]
        ]
    }
    _send_message(
        db,
        chat_id,
        f"⚠️ Delete <b>{agent_name}</b>? This cannot be undone.",
        reply_markup=keyboard,
    )


def _render_telegram_group_picker(
    db: Session,
    chat_id: str,
    agent_id: int,
    prompt: str,
    groups: list[dict],
    invite_url: str | None,
) -> None:
    """Render an inline keyboard listing the user's known Telegram groups
    plus a one-tap "add me to a new group" URL button.

    Group buttons use callback_data ``grouppick:<agent_id>:<idx>`` —
    we keep callback_data short (Telegram caps at 64 bytes) and stash
    the actual chat_id list in Redis under a per-message key.
    """
    rows: list[list[dict]] = []
    # Stash the (idx → chat_id) map in Redis so the callback handler can
    # resolve it. We avoid putting chat_ids directly in callback_data
    # because chat_ids can be long negatives (-100…) and we'd blow the
    # 64-byte cap quickly when combined with the prefix + agent_id.
    if groups:
        try:
            mapping = {str(i): g["chat_id"] for i, g in enumerate(groups)}
            get_redis().setex(
                f"telegram:group_pick:{chat_id}:{agent_id}",
                settings.telegram_prompt_ttl_seconds,
                json.dumps(mapping),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "telegram_group_pick_store_failed",
                extra={"chat_id": chat_id, "agent_id": agent_id},
            )

        for i, g in enumerate(groups[:10]):
            title = (g.get("title") or g.get("chat_id") or "Group")[:48]
            rows.append(
                [
                    {
                        "text": f"💬 {title}",
                        "callback_data": f"grouppick:{agent_id}:{i}",
                    }
                ]
            )

    # Stash an "awaiting group" pointer keyed by the user's tg id (== DM
    # chat_id) so `my_chat_member` can auto-bind the next group the user
    # adds the bot to, without forcing a second tap in the DM.
    try:
        get_redis().setex(
            f"telegram:awaiting_group:{chat_id}",
            settings.telegram_prompt_ttl_seconds,
            str(agent_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "telegram_awaiting_group_store_failed",
            extra={"chat_id": chat_id, "agent_id": agent_id},
        )

    if invite_url:
        rows.append(
            [
                {
                    "text": "➕ Add me to a new group",
                    "url": invite_url,
                }
            ]
        )

    rows.append(
        [{"text": "Cancel", "callback_data": f"groupcancel:{agent_id}"}]
    )

    body = prompt
    if not groups and invite_url:
        body += (
            "\n\nI don't see any groups yet. Tap "
            "<b>➕ Add me to a new group</b> below — "
            "I'll auto-bind it once you've added me."
        )
    _send_message(db, chat_id, body, reply_markup={"inline_keyboard": rows})


def _bind_group_to_agent(
    db: Session, *, user_id: int, agent: "Agent", group_chat_id: str
) -> None:
    """Persist agent.config.telegram_chat_id = group_chat_id and ensure a
    daily SummarySchedule exists. Caller is responsible for db.commit()."""
    from app.models.summary_schedule import SummarySchedule

    try:
        cfg = json.loads(agent.config) if agent.config else {}
    except Exception:  # noqa: BLE001
        cfg = {}
    cfg["telegram_chat_id"] = str(group_chat_id)
    agent.config = json.dumps(cfg)
    db.add(agent)

    existing = db.execute(
        select(SummarySchedule).where(
            SummarySchedule.user_id == user_id,
            SummarySchedule.chat_id == str(group_chat_id),
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            SummarySchedule(
                user_id=user_id,
                chat_id=str(group_chat_id),
                timezone="UTC",
                send_hour=18,
                send_minute=0,
                active=True,
            )
        )
    else:
        existing.active = True
        db.add(existing)


def _agent_manage_keyboard(agent_id: int) -> dict:
    """Inline keyboard shown alongside the bind-success confirmation so
    the user can tweak schedule / pause / delete without remembering
    text commands."""
    return {
        "inline_keyboard": [
            [
                {"text": "🕒 Change time", "callback_data": f"agtime:{agent_id}"},
                {"text": "⏸ Pause", "callback_data": f"agpause:{agent_id}"},
            ],
            [
                {"text": "🗑 Delete agent", "callback_data": f"delpick:{agent_id}"},
            ],
        ]
    }


# ---- Welcome / help text --------------------------------------------------
# Telegram auto-linkifies any "/command" token in plain message text, so we
# can give users a BotFather-style menu just by listing commands here.

_WELCOME_TEXT = (
    "👋 <b>Welcome to OpenClaw</b> — your AI agent automation operator.\n"
    "\n"
    "I help you create, run, and manage automation agents that work with "
    "Gmail, Google Calendar, Telegram, and more.\n"
    "\n"
    "<b>You can control me by sending these commands:</b>\n"
    "\n"
    "/agents — list your agents\n"
    "/newagent — create a new agent from a template\n"
    "/run — run an agent: <code>/run &lt;name&gt; &lt;prompt&gt;</code>\n"
    "/delete — delete one of your agents\n"
    "/summary_now — generate an instant summary\n"
    "/help — show this menu again\n"
    "\n"
    "<b>Or just chat naturally</b> — try:\n"
    "• <i>create an agent that summarizes my unread emails</i>\n"
    "• <i>list all my agents</i>\n"
    "• <i>delete my old test agent</i>\n"
)

_HELP_TEXT = (
    "<b>OpenClaw commands</b>\n"
    "\n"
    "/agents — list your agents\n"
    "/newagent — create a new agent\n"
    "/run — <code>/run &lt;name&gt; &lt;prompt&gt;</code>\n"
    "/delete — delete one of your agents\n"
    "/summary_now — generate an instant summary\n"
    "/start — show the welcome menu\n"
    "/help — show this message\n"
    "\n"
    "You can also just chat naturally — e.g. <i>“create an agent that drafts "
    "replies to unread emails every morning”</i>."
)


def _send_welcome(db: Session, chat_id: str) -> None:
    _send_message(db, chat_id, _WELCOME_TEXT)



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
                "allowed_updates": ["message", "edited_message", "callback_query", "my_chat_member"],
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

    # Best-effort: also publish the slash-command menu so users see hints
    # in their Telegram client. Don't fail the whole call if this errors.
    commands_result: dict | None = None
    try:
        commands_result = _set_bot_commands(bot_token)
    except Exception:  # noqa: BLE001
        logger.exception("telegram_set_commands_failed")

    return {
        "ok": True,
        "webhook_url": webhook_url,
        "telegram_response": data,
        "commands_response": commands_result,
    }


_DEFAULT_BOT_COMMANDS: list[dict] = [
    {"command": "start", "description": "Link your account / show welcome"},
    {"command": "help", "description": "Show available commands"},
    {"command": "agents", "description": "List your agents"},
    {"command": "newagent", "description": "Create a new agent"},
    {"command": "run", "description": "Run an agent: /run <name> <prompt>"},
    {"command": "delete", "description": "Delete one of your agents"},
    {"command": "summary_now", "description": "Generate an instant summary"},
]


def _set_bot_commands(bot_token: str) -> dict:
    """Register the slash-command menu Telegram shows in the chat input.

    Idempotent. Returns Telegram's response dict.
    """
    resp = httpx.post(
        f"https://api.telegram.org/bot{bot_token}/setMyCommands",
        json={"commands": _DEFAULT_BOT_COMMANDS},
        timeout=15,
    )
    return resp.json()


@router.post("/admin/set-commands")
def set_telegram_commands(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Republish the slash-command menu in Telegram. Useful after editing
    the command list. Idempotent."""
    bot_token = _get_bot_token(db)
    if not bot_token:
        raise HTTPException(status_code=400, detail="telegram_bot_token not configured")
    try:
        result = _set_bot_commands(bot_token)
    except Exception as exc:  # pragma: no cover - network failure path
        logger.exception("telegram_set_commands_failed")
        raise HTTPException(status_code=502, detail=f"telegram_api_error: {exc}")
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    record_audit(
        db,
        user_id=current_user.id,
        action="telegram.commands_set",
        resource_type="telegram_bot",
        resource_id=None,
        metadata={"commands": _DEFAULT_BOT_COMMANDS},
    )
    return {"ok": True, "commands": _DEFAULT_BOT_COMMANDS, "telegram_response": result}


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
    my_chat_member = update.get("my_chat_member")
    if not message and not callback and not my_chat_member:
        return {"ok": True}

    # ---- Bot was added/removed from a group ------------------------------
    # Telegram doesn't auto-redirect the user back to our DM after they
    # add us, so we proactively DM them so the flow doesn't feel broken.
    if my_chat_member and not callback:
        try:
            new_status = (my_chat_member.get("new_chat_member") or {}).get("status")
            old_status = (my_chat_member.get("old_chat_member") or {}).get("status")
            chat_blob = my_chat_member.get("chat") or {}
            chat_kind = chat_blob.get("type")
            group_chat_id = chat_blob.get("id")
            group_title = chat_blob.get("title") or "your group"
            adder = my_chat_member.get("from") or {}
            adder_tg_id = str(adder.get("id") or "")

            became_member = (
                new_status in {"member", "administrator"}
                and old_status in {None, "left", "kicked"}
                and chat_kind in {"group", "supergroup"}
            )
            if became_member and adder_tg_id:
                link = db.execute(
                    select(TelegramLink).where(
                        TelegramLink.telegram_user_id == adder_tg_id
                    )
                ).scalar_one_or_none()
                if link and link.telegram_user_id:
                    dm_chat_id = str(link.telegram_user_id)
                    user_id_int = link.user_id

                    # 1. Persist group membership immediately (idempotent)
                    #    so the picker / future flows can list it.
                    try:
                        existing_marker = db.execute(
                            select(TelegramMessage)
                            .where(
                                TelegramMessage.user_id == user_id_int,
                                TelegramMessage.chat_id == str(group_chat_id),
                            )
                            .limit(1)
                        ).scalar_one_or_none()
                        if existing_marker is None:
                            marker_payload = {
                                "chat": {
                                    "id": group_chat_id,
                                    "type": chat_kind,
                                    "title": group_title,
                                },
                                "_marker": "bot_joined",
                            }
                            db.add(
                                TelegramMessage(
                                    user_id=user_id_int,
                                    chat_id=str(group_chat_id),
                                    chat_type=chat_kind,
                                    message_id=f"join-{my_chat_member.get('date') or ''}",
                                    sender_id=str(adder_tg_id),
                                    sender_name=(adder.get("first_name") or "")[:200],
                                    text=None,
                                    sent_at=None,
                                    raw_json=json.dumps(
                                        marker_payload, ensure_ascii=True
                                    ),
                                )
                            )
                            db.commit()
                    except Exception:  # noqa: BLE001
                        db.rollback()
                        logger.exception(
                            "telegram_join_marker_write_failed",
                            extra={
                                "chat_id": str(group_chat_id),
                                "user_id": user_id_int,
                            },
                        )

                    # 2. Auto-bind: if the user was mid-flow waiting to
                    #    pick a group for an agent, bind it now and DM
                    #    one clean success message. No "open chat to
                    #    continue" noise — they don't need to do anything.
                    pending_agent_id: int | None = None
                    try:
                        raw = get_redis().get(
                            f"telegram:awaiting_group:{dm_chat_id}"
                        )
                        if raw:
                            pending_agent_id = int(raw)
                    except Exception:  # noqa: BLE001
                        pending_agent_id = None

                    if pending_agent_id is not None:
                        agent = db.execute(
                            select(Agent).where(
                                Agent.id == pending_agent_id,
                                Agent.user_id == user_id_int,
                            )
                        ).scalar_one_or_none()
                        if agent is not None:
                            try:
                                _bind_group_to_agent(
                                    db,
                                    user_id=user_id_int,
                                    agent=agent,
                                    group_chat_id=str(group_chat_id),
                                )
                                db.commit()
                            except Exception:  # noqa: BLE001
                                db.rollback()
                                logger.exception(
                                    "telegram_autobind_failed",
                                    extra={
                                        "user_id": user_id_int,
                                        "agent_id": pending_agent_id,
                                        "group_chat_id": str(group_chat_id),
                                    },
                                )
                            else:
                                try:
                                    get_redis().delete(
                                        f"telegram:awaiting_group:{dm_chat_id}"
                                    )
                                    get_redis().delete(
                                        f"telegram:group_pick:{dm_chat_id}:{agent.id}"
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                _send_message(
                                    db,
                                    dm_chat_id,
                                    f"✅ <b>{agent.name}</b> is now monitoring "
                                    f"<b>{group_title}</b>.\n"
                                    "Daily summary at <b>6 PM UTC</b> — "
                                    "DM'd to you.",
                                    reply_markup=_agent_manage_keyboard(agent.id),
                                )
                                return {"ok": True}

                    # 3. No pending agent — send a tiny silent-friendly
                    #    DM so the user knows we joined, but no buttons,
                    #    no "open chat to continue" since they're already
                    #    here.
                    _send_message(
                        db,
                        dm_chat_id,
                        f"✅ Added to <b>{group_title}</b>. I'll quietly "
                        "log messages there for future summaries.",
                    )
        except Exception:  # noqa: BLE001 — best-effort notice
            logger.exception(
                "telegram_my_chat_member_handle_failed"
            )
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

        # ---- Agent delete callbacks (from unified ChatService picker) ----
        if data in {"delcancel", "runcancel"}:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            if not link:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            if message_id is not None:
                _edit_message_text(
                    db, str(chat_id), int(message_id), "Cancelled.", reply_markup={"inline_keyboard": []}
                )
            if data == "runcancel":
                # Drop any pending run-prompt state for this chat.
                try:
                    get_redis().delete(f"telegram:pending_run:{chat_id}")
                except Exception:  # noqa: BLE001
                    pass
            if callback_id:
                _answer_callback(db, callback_id, "Cancelled")
            return {"ok": True}

        if parts[0] == "runpick" and len(parts) >= 2:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            user_id = link.user_id if link else None
            if not user_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id = int(parts[1])
            except ValueError:
                if callback_id:
                    _answer_callback(db, callback_id, "Bad selection")
                return {"ok": True}
            agent = db.execute(
                select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id)
            ).scalar_one_or_none()
            if not agent:
                if callback_id:
                    _answer_callback(db, callback_id, "Agent not found")
                return {"ok": True}
            # Stash the pending run so the user's next message becomes the
            # prompt. 5-min TTL is plenty for a normal back-and-forth.
            try:
                get_redis().setex(
                    f"telegram:pending_run:{chat_id}",
                    settings.telegram_prompt_ttl_seconds,
                    json.dumps({"agent_id": agent.id, "agent_name": agent.name}),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "telegram_pending_run_store_failed",
                    extra={"chat_id": str(chat_id), "agent_id": agent.id},
                )
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            confirmation = (
                f"▶️ Selected <b>{agent.name}</b>.\n"
                "Now send the prompt you want it to run."
            )
            if message_id is not None:
                _edit_message_text(
                    db, str(chat_id), int(message_id), confirmation, reply_markup={"inline_keyboard": []}
                )
            else:
                _send_message(db, str(chat_id), confirmation)
            if callback_id:
                _answer_callback(db, callback_id, agent.name)
            return {"ok": True}

        # ---- Resume / discard incomplete agent ---------------------------
        if parts[0] in {"agpause", "agtime"} and len(parts) >= 2:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            user_id = link.user_id if link else None
            if not user_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id_int = int(parts[1])
            except ValueError:
                if callback_id:
                    _answer_callback(db, callback_id, "Bad selection")
                return {"ok": True}
            agent = db.execute(
                select(Agent).where(
                    Agent.id == agent_id_int, Agent.user_id == user_id
                )
            ).scalar_one_or_none()
            if not agent:
                if callback_id:
                    _answer_callback(db, callback_id, "Agent not found")
                return {"ok": True}

            from app.models.summary_schedule import SummarySchedule

            try:
                cfg = json.loads(agent.config) if agent.config else {}
            except Exception:  # noqa: BLE001
                cfg = {}
            bound_chat = cfg.get("telegram_chat_id")

            if parts[0] == "agpause":
                if not bound_chat:
                    if callback_id:
                        _answer_callback(db, callback_id, "No schedule yet")
                    return {"ok": True}
                schedule = db.execute(
                    select(SummarySchedule).where(
                        SummarySchedule.user_id == user_id,
                        SummarySchedule.chat_id == str(bound_chat),
                    )
                ).scalar_one_or_none()
                if schedule is None:
                    if callback_id:
                        _answer_callback(db, callback_id, "No schedule")
                    return {"ok": True}
                schedule.active = not bool(schedule.active)
                db.add(schedule)
                try:
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                    logger.exception(
                        "telegram_pause_failed",
                        extra={"user_id": user_id, "agent_id": agent_id_int},
                    )
                    if callback_id:
                        _answer_callback(db, callback_id, "Save failed")
                    return {"ok": True}
                state = "paused" if not schedule.active else "resumed"
                _send_message(
                    db,
                    str(chat_id),
                    f"⏸ <b>{agent.name}</b> {state}." if state == "paused"
                    else f"▶️ <b>{agent.name}</b> {state}.",
                    reply_markup=_agent_manage_keyboard(agent.id),
                )
                if callback_id:
                    _answer_callback(db, callback_id, state.title())
                return {"ok": True}

            # parts[0] == "agtime" — show preset hour buttons.
            preset_hours = [6, 9, 12, 15, 18, 21]
            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": f"{h:02d}:00 UTC",
                            "callback_data": f"agtset:{agent_id_int}:{h}",
                        }
                        for h in preset_hours[:3]
                    ],
                    [
                        {
                            "text": f"{h:02d}:00 UTC",
                            "callback_data": f"agtset:{agent_id_int}:{h}",
                        }
                        for h in preset_hours[3:]
                    ],
                    [{"text": "Cancel", "callback_data": f"agtcancel:{agent_id_int}"}],
                ]
            }
            _send_message(
                db,
                str(chat_id),
                f"🕒 Pick a new send time for <b>{agent.name}</b>:",
                reply_markup=keyboard,
            )
            if callback_id:
                _answer_callback(db, callback_id, "Pick a time")
            return {"ok": True}

        if parts[0] == "agtcancel" and len(parts) >= 2:
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            if message_id is not None:
                _edit_message_text(
                    db,
                    str(chat_id),
                    int(message_id),
                    "Cancelled.",
                    reply_markup={"inline_keyboard": []},
                )
            if callback_id:
                _answer_callback(db, callback_id, "Cancelled")
            return {"ok": True}

        if parts[0] == "agtset" and len(parts) >= 3:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            user_id = link.user_id if link else None
            if not user_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id_int = int(parts[1])
                hour_int = int(parts[2])
            except (ValueError, IndexError):
                if callback_id:
                    _answer_callback(db, callback_id, "Bad selection")
                return {"ok": True}
            if not 0 <= hour_int <= 23:
                if callback_id:
                    _answer_callback(db, callback_id, "Bad hour")
                return {"ok": True}
            agent = db.execute(
                select(Agent).where(
                    Agent.id == agent_id_int, Agent.user_id == user_id
                )
            ).scalar_one_or_none()
            if not agent:
                if callback_id:
                    _answer_callback(db, callback_id, "Agent not found")
                return {"ok": True}
            try:
                cfg = json.loads(agent.config) if agent.config else {}
            except Exception:  # noqa: BLE001
                cfg = {}
            bound_chat = cfg.get("telegram_chat_id")
            if not bound_chat:
                if callback_id:
                    _answer_callback(db, callback_id, "No group bound")
                return {"ok": True}

            from app.models.summary_schedule import SummarySchedule

            schedule = db.execute(
                select(SummarySchedule).where(
                    SummarySchedule.user_id == user_id,
                    SummarySchedule.chat_id == str(bound_chat),
                )
            ).scalar_one_or_none()
            if schedule is None:
                schedule = SummarySchedule(
                    user_id=user_id,
                    chat_id=str(bound_chat),
                    timezone="UTC",
                    send_hour=hour_int,
                    send_minute=0,
                    active=True,
                )
            else:
                schedule.send_hour = hour_int
                schedule.send_minute = 0
                schedule.active = True
            db.add(schedule)
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "telegram_settime_failed",
                    extra={"user_id": user_id, "agent_id": agent_id_int},
                )
                if callback_id:
                    _answer_callback(db, callback_id, "Save failed")
                return {"ok": True}
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            confirmation = (
                f"✅ <b>{agent.name}</b> will now send at <b>{hour_int:02d}:00 UTC</b>."
            )
            if message_id is not None:
                _edit_message_text(
                    db,
                    str(chat_id),
                    int(message_id),
                    confirmation,
                    reply_markup=_agent_manage_keyboard(agent.id),
                )
            else:
                _send_message(
                    db,
                    str(chat_id),
                    confirmation,
                    reply_markup=_agent_manage_keyboard(agent.id),
                )
            if callback_id:
                _answer_callback(db, callback_id, "Updated")
            return {"ok": True}
        if parts[0] in {"resume", "discard"} and len(parts) >= 2:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            user_id = link.user_id if link else None
            if not user_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id_int = int(parts[1])
            except ValueError:
                if callback_id:
                    _answer_callback(db, callback_id, "Bad selection")
                return {"ok": True}
            agent = db.execute(
                select(Agent).where(
                    Agent.id == agent_id_int, Agent.user_id == user_id
                )
            ).scalar_one_or_none()
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")

            if parts[0] == "discard":
                from app.services.agent_deleter import delete_agent_cascade

                deleted_name = None
                if agent is not None:
                    try:
                        deleted_name = delete_agent_cascade(
                            db, user_id=user_id, agent_id=agent_id_int
                        )
                        db.commit()
                    except Exception:  # noqa: BLE001
                        db.rollback()
                        logger.exception(
                            "agent_discard_failed",
                            extra={"user_id": user_id, "agent_id": agent_id_int},
                        )
                        if callback_id:
                            _answer_callback(db, callback_id, "Delete failed")
                        return {"ok": True}
                confirmation = (
                    f"🗑 Deleted <b>{deleted_name}</b>. Send your "
                    "create-agent message again to start fresh."
                    if deleted_name
                    else "That agent was already gone. Send your create-agent message again."
                )
                if message_id is not None:
                    _edit_message_text(
                        db,
                        str(chat_id),
                        int(message_id),
                        confirmation,
                        reply_markup={"inline_keyboard": []},
                    )
                else:
                    _send_message(db, str(chat_id), confirmation)
                if callback_id:
                    _answer_callback(db, callback_id, "Deleted")
                return {"ok": True}

            # parts[0] == "resume" → re-render the group picker for this agent.
            if agent is None:
                if callback_id:
                    _answer_callback(db, callback_id, "Agent not found")
                _send_message(
                    db,
                    str(chat_id),
                    "I couldn't find that agent anymore. Try creating it again.",
                )
                return {"ok": True}
            from app.services.agent_builder import (
                _build_telegram_group_picker_action,
            )

            picker = _build_telegram_group_picker_action(
                db, user_id, agent.id, agent.name
            )
            if message_id is not None:
                _edit_message_text(
                    db,
                    str(chat_id),
                    int(message_id),
                    f"Resuming setup for <b>{agent.name}</b>…",
                    reply_markup={"inline_keyboard": []},
                )
            if picker:
                _render_telegram_group_picker(
                    db,
                    str(chat_id),
                    int(picker.get("agent_id")),
                    str(picker.get("prompt") or "Pick a group"),
                    list(picker.get("groups") or []),
                    picker.get("invite_url"),
                )
            else:
                _send_message(
                    db,
                    str(chat_id),
                    "I can't render the group picker right now. Add me to a "
                    "group manually and try <code>run "
                    f"{agent.name}</code>.",
                )
            if callback_id:
                _answer_callback(db, callback_id, "Resumed")
            return {"ok": True}

        # ---- Telegram group picker (after agent create) ------------------
        if parts[0] == "groupcancel" and len(parts) >= 2:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            if not link:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id_int = int(parts[1])
            except ValueError:
                agent_id_int = 0
            try:
                get_redis().delete(
                    f"telegram:group_pick:{chat_id}:{agent_id_int}"
                )
            except Exception:  # noqa: BLE001
                pass
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            if message_id is not None:
                _edit_message_text(
                    db,
                    str(chat_id),
                    int(message_id),
                    "Cancelled. You can run this agent later by typing "
                    "<code>run &lt;agent name&gt;</code>.",
                    reply_markup={"inline_keyboard": []},
                )
            if callback_id:
                _answer_callback(db, callback_id, "Cancelled")
            return {"ok": True}

        if parts[0] == "grouppick" and len(parts) >= 3:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            user_id = link.user_id if link else None
            if not user_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id_int = int(parts[1])
                idx = parts[2]
            except (ValueError, IndexError):
                if callback_id:
                    _answer_callback(db, callback_id, "Bad selection")
                return {"ok": True}

            # Resolve idx → real chat_id from Redis stash.
            picked_chat_id: str | None = None
            try:
                raw = get_redis().get(
                    f"telegram:group_pick:{chat_id}:{agent_id_int}"
                )
                if raw:
                    mapping = json.loads(raw)
                    picked_chat_id = mapping.get(str(idx))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "telegram_group_pick_load_failed",
                    extra={"chat_id": str(chat_id), "agent_id": agent_id_int},
                )

            if not picked_chat_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Selection expired")
                _send_message(
                    db,
                    str(chat_id),
                    "That picker expired. Please re-create the agent or "
                    "send <code>run &lt;agent name&gt;</code> again.",
                )
                return {"ok": True}

            agent = db.execute(
                select(Agent).where(
                    Agent.id == agent_id_int, Agent.user_id == user_id
                )
            ).scalar_one_or_none()
            if not agent:
                if callback_id:
                    _answer_callback(db, callback_id, "Agent not found")
                return {"ok": True}

            # Bind chat_id into agent.config (JSON) and create/upsert a
            # daily SummarySchedule for it.
            try:
                _bind_group_to_agent(
                    db, user_id=user_id, agent=agent, group_chat_id=picked_chat_id
                )
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "telegram_group_pick_commit_failed",
                    extra={
                        "chat_id": str(chat_id),
                        "agent_id": agent_id_int,
                        "picked_chat_id": picked_chat_id,
                    },
                )
                if callback_id:
                    _answer_callback(db, callback_id, "Save failed")
                return {"ok": True}

            try:
                get_redis().delete(
                    f"telegram:group_pick:{chat_id}:{agent_id_int}"
                )
                get_redis().delete(f"telegram:awaiting_group:{chat_id}")
            except Exception:  # noqa: BLE001
                pass

            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            confirmation = (
                f"✅ <b>{agent.name}</b> is now monitoring this group.\n"
                "Daily summary at <b>6 PM UTC</b> — DM'd to you."
            )
            keyboard = _agent_manage_keyboard(agent.id)
            if message_id is not None:
                _edit_message_text(
                    db,
                    str(chat_id),
                    int(message_id),
                    confirmation,
                    reply_markup=keyboard,
                )
            else:
                _send_message(
                    db, str(chat_id), confirmation, reply_markup=keyboard
                )
            if callback_id:
                _answer_callback(db, callback_id, "Bound")
            return {"ok": True}

        if parts[0] in {"delpick", "delconfirm"} and len(parts) >= 2:
            link = db.execute(
                select(TelegramLink).where(TelegramLink.telegram_user_id == telegram_user_id)
            ).scalar_one_or_none()
            user_id = link.user_id if link else None
            if not user_id:
                if callback_id:
                    _answer_callback(db, callback_id, "Please link your account first")
                return {"ok": True}
            try:
                agent_id = int(parts[1])
            except ValueError:
                if callback_id:
                    _answer_callback(db, callback_id, "Bad selection")
                return {"ok": True}

            if parts[0] == "delpick":
                # User picked an agent from the picker → ask to confirm.
                agent = db.execute(
                    select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id)
                ).scalar_one_or_none()
                if not agent:
                    if callback_id:
                        _answer_callback(db, callback_id, "Agent not found")
                    return {"ok": True}
                _render_delete_confirm(db, str(chat_id), agent.id, agent.name)
                if callback_id:
                    _answer_callback(db, callback_id, agent.name)
                return {"ok": True}

            # parts[0] == "delconfirm" → actually delete.
            from app.services.agent_deleter import delete_agent_cascade

            try:
                deleted_name = delete_agent_cascade(
                    db, user_id=user_id, agent_id=agent_id
                )
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "agent_delete_failed",
                    extra={"user_id": user_id, "agent_id": agent_id},
                )
                if callback_id:
                    _answer_callback(db, callback_id, "Delete failed")
                _send_message(
                    db,
                    str(chat_id),
                    "⚠️ Couldn't delete that agent. Please try again.",
                )
                return {"ok": True}
            message_obj = callback.get("message") or {}
            message_id = message_obj.get("message_id")
            confirmation = (
                f"✅ <b>{deleted_name}</b> deleted."
                if deleted_name
                else "That agent was already gone."
            )
            if message_id is not None:
                _edit_message_text(
                    db, str(chat_id), int(message_id), confirmation, reply_markup={"inline_keyboard": []}
                )
            else:
                _send_message(db, str(chat_id), confirmation)
            if callback_id:
                _answer_callback(db, callback_id, "Deleted")
            return {"ok": True}

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
            _send_message(db, chat_id, "✅ Account linked.")
            _send_welcome(db, str(chat_id))
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

    # ------------------------------------------------------------------
    # Group / supergroup scope guard.
    # The bot exists in groups *only* to read messages so it can summarise
    # them later. It must NEVER send anything in a group — no chat, no
    # /start reply, no run output. All UX happens in the user's DM.
    # /start in a group is therefore silently ignored (we don't even log
    # it as content); every other group message is logged for the daily
    # summary, then we exit.
    # ------------------------------------------------------------------
    if chat_type in {"group", "supergroup"}:
        stripped_in_group = (text or "").strip().lower()
        bot_uname = (_get_bot_username(db) or "").lower()
        is_command_for_us = (
            stripped_in_group.startswith("/")
            and (
                bot_uname == ""
                or stripped_in_group.endswith(f"@{bot_uname}")
                or "@" not in stripped_in_group
            )
        )
        if is_command_for_us:
            # Silently ignore — we never reply in groups.
            return {"ok": True}

        # Log every other group message for the daily summary.
        try:
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
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "telegram_group_message_log_failed",
                extra={"chat_id": str(chat_id)},
            )
        return {"ok": True}

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

    # ----------------------------------------------------------------------
    # Linked user just sent /start or /help — show the welcome menu BEFORE
    # we route to the unified ChatService so it doesn't end up at the LLM.
    # Also clear any stale pending-run state so the next message isn't
    # accidentally consumed as a prompt for a previously-picked agent.
    # ----------------------------------------------------------------------
    stripped_early = (text or "").strip().lower()
    if stripped_early in {"/start", "/start@" + (_get_bot_username(db) or "").lower()}:
        try:
            get_redis().delete(f"telegram:pending_run:{chat_id}")
        except Exception:  # noqa: BLE001
            pass
        _send_welcome(db, str(chat_id))
        return {"ok": True}
    if stripped_early in {"/help", "/commands"}:
        try:
            get_redis().delete(f"telegram:pending_run:{chat_id}")
        except Exception:  # noqa: BLE001
            pass
        _send_message(db, str(chat_id), _HELP_TEXT)
        return {"ok": True}

    # ----------------------------------------------------------------------
    # Pending-run resolver: user previously picked an agent via the run
    # picker — their next non-slash message becomes the run prompt.
    # ----------------------------------------------------------------------
    if text and not text.strip().startswith("/"):
        try:
            pending_run_raw = get_redis().get(f"telegram:pending_run:{chat_id}")
        except Exception:  # noqa: BLE001
            pending_run_raw = None
        if pending_run_raw:
            try:
                pending_run = json.loads(pending_run_raw)
                pending_agent_id = int(pending_run.get("agent_id"))
            except Exception:  # noqa: BLE001
                pending_agent_id = 0
            if pending_agent_id:
                agent = db.execute(
                    select(Agent).where(
                        Agent.id == pending_agent_id, Agent.user_id == linked_user_id
                    )
                ).scalar_one_or_none()
                # Always clear the pending state — one-shot.
                try:
                    get_redis().delete(f"telegram:pending_run:{chat_id}")
                except Exception:  # noqa: BLE001
                    pass
                if not agent:
                    _send_message(db, str(chat_id), "That agent is no longer available.")
                    return {"ok": True}
                _send_chat_action(db, str(chat_id), "typing")
                try:
                    run = execute_agent_run(
                        db, agent, linked_user_id, text.strip(), source="telegram:pickrun"
                    )
                except AgentRuntimeError as exc:
                    _send_message(db, str(chat_id), f"⚠️ Couldn't run {agent.name}: {exc}")
                    return {"ok": True}
                _send_message(
                    db,
                    str(chat_id),
                    f"✅ <b>{agent.name}</b> finished.\n\n{run.output_text or '(no output)'}",
                )
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
        # Show "... is typing" while we run the model so the user knows
        # the bot is working. Best-effort — silent on failure.
        _send_chat_action(db, str(chat_id), "typing")
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
                    a_type = action.get("type")
                    if a_type == "permission_request":
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
                    elif a_type == "agent_picker":
                        try:
                            _render_agent_picker(
                                db,
                                str(chat_id),
                                str(action.get("prompt") or "Pick an agent"),
                                list(action.get("agents") or []),
                                str(action.get("action") or "delete"),
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "telegram_picker_render_failed",
                                extra={"chat_id": str(chat_id)},
                            )
                    elif a_type == "agent_delete_confirm":
                        try:
                            _render_delete_confirm(
                                db,
                                str(chat_id),
                                int(action.get("agent_id")),
                                str(action.get("agent_name") or "this agent"),
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "telegram_delete_confirm_render_failed",
                                extra={"chat_id": str(chat_id)},
                            )
                    elif a_type == "telegram_group_picker":
                        try:
                            _render_telegram_group_picker(
                                db,
                                str(chat_id),
                                int(action.get("agent_id")),
                                str(action.get("prompt") or "Pick a group"),
                                list(action.get("groups") or []),
                                action.get("invite_url"),
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "telegram_group_picker_render_failed",
                                extra={"chat_id": str(chat_id)},
                            )
                    elif a_type == "agent_resume_prompt":
                        try:
                            agent_id_int = int(action.get("agent_id"))
                            keyboard = {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "✅ Yes, continue setup",
                                            "callback_data": f"resume:{agent_id_int}",
                                        },
                                        {
                                            "text": "🗑 No, delete it",
                                            "callback_data": f"discard:{agent_id_int}",
                                        },
                                    ]
                                ]
                            }
                            _send_message(
                                db,
                                str(chat_id),
                                "Pick one:",
                                reply_markup=keyboard,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "telegram_resume_prompt_render_failed",
                                extra={"chat_id": str(chat_id)},
                            )
        except Exception:  # noqa: BLE001 — never break the webhook
            logger.exception(
                "telegram_chat_service_error",
                extra={"chat_id": str(chat_id), "user_id": linked_user_id},
            )
            _send_message(db, chat_id, "Sorry — something went wrong. Please try again.")
        return {"ok": True}

    # Friendly fallback for linked users when unified ChatService is OFF.
    # Without this, bare /start, /help, or any plain text from a linked
    # user produces silence because none of the legacy command branches
    # above match. Always reply so the bot is never mute.
    stripped = (text or "").strip()
    if stripped == "/start":
        _send_welcome(db, str(chat_id))
        return {"ok": True}
    if stripped in {"/help", "/commands"}:
        _send_message(db, str(chat_id), _HELP_TEXT)
        return {"ok": True}
    if stripped.startswith("/"):
        _send_message(
            db,
            chat_id,
            f"Unknown command: <code>{stripped.split()[0]}</code>. Send /help for the list.",
        )
        return {"ok": True}
    if stripped:
        _send_message(
            db,
            chat_id,
            "I received your message. Use /help to see what I can do, or visit the dashboard for full chat.",
        )
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
