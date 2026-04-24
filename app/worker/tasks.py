from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.approval import Approval
from app.services.agent_executor import ToolExecutionError, execute_tool_local
from app.services.agent_runtime import execute_agent_run_by_id
from app.models.agent_run import AgentRun
from app.models.worker_heartbeat import WorkerHeartbeat
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


@celery_app.task(
    name="app.worker.tasks.run_agent_task",
    bind=True,
    soft_time_limit=settings.agent_timeout_seconds,
    time_limit=settings.agent_timeout_seconds + 30,
)
def run_agent_task(self, run_id: int):
    db = SessionLocal()
    try:
        return execute_agent_run_by_id(db, run_id)
    except Exception as exc:
        countdown = settings.agent_task_retry_backoff_seconds * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=settings.agent_task_max_retries)
    finally:
        db.close()


@celery_app.task(
    name="app.worker.tasks.execute_tool_task",
    bind=True,
    soft_time_limit=settings.agent_tool_timeout_seconds,
    time_limit=settings.agent_tool_kill_switch_seconds,
)
def execute_tool_task(self, tool_name: str, tool_args: dict, internal_user_id: int, internal_agent_id: int | None):
    db = SessionLocal()
    try:
        return execute_tool_local(db, tool_name, tool_args, internal_user_id, internal_agent_id)
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


@celery_app.task(name="app.worker.tasks.record_worker_heartbeat")
def record_worker_heartbeat(queue_name: str):
    db = SessionLocal()
    try:
        heartbeat = (
            db.query(WorkerHeartbeat)
            .filter(WorkerHeartbeat.queue_name == queue_name)
            .one_or_none()
        )
        now = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        if heartbeat:
            heartbeat.last_seen = now
        else:
            heartbeat = WorkerHeartbeat(queue_name=queue_name, last_seen=now)
        db.add(heartbeat)
        db.commit()
        return {"queue": queue_name, "last_seen": now.isoformat()}
    finally:
        db.close()


@celery_app.task(name="app.worker.tasks.monitor_stuck_runs")
def monitor_stuck_runs():
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        cutoff = cutoff - timedelta(seconds=settings.agent_run_stuck_seconds)

        stuck_runs = (
            db.query(AgentRun)
            .filter(AgentRun.status == "running", AgentRun.updated_at < cutoff)
            .all()
        )
        for run in stuck_runs:
            if run.requeue_count < settings.agent_run_requeue_limit:
                run.requeue_count += 1
                run.status = "pending"
                db.add(run)
                db.commit()
                celery_app.send_task("app.worker.tasks.run_agent_task", args=[run.id], queue="planner")
            else:
                run.status = "failed"
                run.error_message = "Run stuck and exceeded requeue limit"
                db.add(run)
                db.commit()
        return {"checked": len(stuck_runs)}
    finally:
        db.close()
