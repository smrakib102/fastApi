from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.approval import Approval
from app.services.agent_executor import ToolExecutionError, execute_tool
from app.services.agent_runtime import execute_agent_run_by_id
from app.models.summary_schedule import SummarySchedule
from app.services.summary_service import generate_summary
from app.services.telegram_service import send_message
from app.worker.celery_app import celery_app
from app.core.config import settings


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


@celery_app.task(name="app.worker.tasks.run_agent_task", bind=True)
def run_agent_task(self, run_id: int):
    db = SessionLocal()
    try:
        return execute_agent_run_by_id(db, run_id)
    except Exception as exc:
        countdown = settings.agent_task_retry_backoff_seconds * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=settings.agent_task_max_retries)
    finally:
        db.close()


@celery_app.task(name="app.worker.tasks.execute_tool_task", bind=True)
def execute_tool_task(self, tool_name: str, tool_args: dict):
    db = SessionLocal()
    try:
        return execute_tool(db, tool_name, tool_args, retries=1)
    except ToolExecutionError as exc:
        countdown = settings.agent_task_retry_backoff_seconds * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=settings.agent_task_max_retries)
    finally:
        db.close()


@celery_app.task(name="app.worker.tasks.approval_wait_task")
def approval_wait_task(approval_id: int):
    db = SessionLocal()
    try:
        approval = db.query(Approval).filter(Approval.id == approval_id).one_or_none()
        if not approval:
            return {"status": "missing"}
        return {"status": approval.status}
    finally:
        db.close()
