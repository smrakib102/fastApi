from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ai_keys import get_code_provider, get_default_provider, get_user_key
from app.core.llm_client import call_gemini, call_openai_chat
from app.core.model_routing import resolve_provider
from app.models.telegram_message import TelegramMessage
from app.services.usage_limits import check_and_record_usage

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-1.5-flash"


def _get_provider(db: Session, user_id: int) -> tuple[str, str]:
    default_provider = get_default_provider(db)
    code_provider = get_code_provider(db)
    provider = resolve_provider(db, user_id, "summary", "summary", default_provider, code_provider)
    if not provider:
        raise RuntimeError("No model provider available")
    model = OPENAI_DEFAULT_MODEL if provider == "openai" else GEMINI_DEFAULT_MODEL
    return provider, model


def generate_summary(
    db: Session,
    user_id: int,
    chat_id: str,
    timezone_name: str,
    now: datetime | None = None,
) -> str:
    tz = ZoneInfo(timezone_name)
    now = now or datetime.now(tz)
    start = (now - timedelta(days=1)).astimezone(timezone.utc)

    messages = db.execute(
        select(TelegramMessage)
        .where(
            TelegramMessage.user_id == user_id,
            TelegramMessage.chat_id == chat_id,
            TelegramMessage.sent_at >= start,
        )
        .order_by(TelegramMessage.sent_at.asc())
    ).scalars().all()

    if not messages:
        return "No messages to summarize for the last 24 hours."

    transcript = "\n".join(
        [f"{msg.sender_name or 'unknown'}: {msg.text or ''}" for msg in messages]
    )
    system_prompt = "Summarize the key points and action items from this Telegram chat."
    user_prompt = f"Chat transcript:\n{transcript}\n\nProvide a concise summary."

    provider, model = _get_provider(db, user_id)
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        raise RuntimeError(f"Missing API key for {provider}")

    if provider == "openai":
        output, tokens = call_openai_chat(
            api_key,
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        check_and_record_usage(db, user_id, provider, tokens)
        return output

    prompt = f"{system_prompt}\n\n{user_prompt}"
    output, tokens = call_gemini(api_key, model, prompt)
    check_and_record_usage(db, user_id, provider, tokens)
    return output
