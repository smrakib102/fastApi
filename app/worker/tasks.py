from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.summary_schedule import SummarySchedule
from app.services.summary_service import generate_summary
from app.services.telegram_service import send_message
from app.worker.celery_app import celery_app


@celery_app.task(name="app.worker.tasks.ping")
def ping():
    return "pong"


@celery_app.task(name="app.worker.tasks.send_summaries")
def send_summaries():
    db = SessionLocal()
    try:
        schedules = db.execute(
            select(SummarySchedule).where(SummarySchedule.active.is_(True))
        ).scalars().all()
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))

        for schedule in schedules:
            tz = ZoneInfo(schedule.timezone)
            now_local = now_utc.astimezone(tz)
            if now_local.hour != schedule.send_hour or now_local.minute < schedule.send_minute:
                continue

            last_sent = schedule.last_sent_at
            if last_sent:
                last_local = last_sent.astimezone(tz)
                if last_local.date() == now_local.date():
                    continue

            try:
                summary = generate_summary(db, schedule.user_id, schedule.chat_id, schedule.timezone)
                send_message(db, schedule.chat_id, summary)
                schedule.last_sent_at = now_utc
                db.add(schedule)
            except Exception:
                continue
        db.commit()
    finally:
        db.close()
