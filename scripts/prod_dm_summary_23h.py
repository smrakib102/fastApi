from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.ai_keys import get_code_provider, get_default_provider, get_user_key
from app.core.llm_client import call_gemini, call_openai_chat
from app.core.model_routing import resolve_provider
from app.db.session import SessionLocal
from app.models.telegram_link import TelegramLink
from app.models.telegram_message import TelegramMessage
from app.services.telegram_service import send_message
from app.services.usage_limits import check_and_record_usage

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

CHAT_ID = "-1003816453990"


def main() -> None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=23)

    with SessionLocal() as db:
        link = db.execute(
            select(TelegramLink).order_by(TelegramLink.id.asc())
        ).scalar_one_or_none()
        if not link:
            raise SystemExit("no telegram link found")

        user_id = link.user_id
        messages = db.execute(
            select(TelegramMessage)
            .where(
                TelegramMessage.user_id == user_id,
                TelegramMessage.chat_id == CHAT_ID,
                TelegramMessage.sent_at >= start,
            )
            .order_by(TelegramMessage.sent_at.asc())
        ).scalars().all()

        if not messages:
            summary = "No messages to summarize for the last 23 hours."
        else:
            transcript = "\n".join(
                [
                    (msg.sender_name or "unknown") + ": " + (msg.text or "")
                    for msg in messages
                ]
            )
            system_prompt = "Summarize the key points and action items from this Telegram chat."
            user_prompt = (
                "Chat transcript (last 23 hours):\n"
                + transcript
                + "\n\nProvide a concise summary."
            )

            default_provider = get_default_provider(db)
            code_provider = get_code_provider(db)
            provider = resolve_provider(
                db, user_id, "summary", "summary", default_provider, code_provider
            )
            if not provider:
                raise SystemExit("No model provider available")

            model = OPENAI_DEFAULT_MODEL if provider == "openai" else GEMINI_DEFAULT_MODEL
            api_key = get_user_key(db, user_id, provider)
            if not api_key:
                raise SystemExit(f"Missing API key for {provider}")

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
                summary = output
            else:
                prompt = system_prompt + "\n\n" + user_prompt
                output, tokens = call_gemini(api_key, model, prompt)
                check_and_record_usage(db, user_id, provider, tokens)
                summary = output

        dm_chat_id = link.telegram_user_id
        send_message(db, dm_chat_id, summary)
        print("sent_to", dm_chat_id)
        print(summary)


if __name__ == "__main__":
    main()
