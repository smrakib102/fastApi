import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.telegram_link import TelegramLink
from app.services.agent_executor import ToolExecutionError, execute_tool_local
from app.services.agent_runtime import execute_agent_run_by_id
from app.models.agent_run import AgentRun
from app.models.worker_heartbeat import WorkerHeartbeat
from app.models.summary_schedule import SummarySchedule
from app.services.summary_service import generate_summary
from app.services.telegram_group_helpers import (
    branded_summary,
    build_group_invite_link,
    is_bot_not_in_chat_error,
)
from app.services.telegram_service import send_message
from app.worker.celery_app import celery_app
from app.core.config import settings


@celery_app.task(name="app.worker.tasks.ping")
def ping():
    return "pong"


def _resolve_agent_name_for_chat(db, user_id: int, chat_id: str) -> str | None:
    """Best-effort: find the user's agent whose config references this chat.

    Returns the agent's name if found, else None. We do a string LIKE on
    the JSON config column — fine at our scale; if user counts grow we'd
    move to a proper FK on SummarySchedule.
    """
    try:
        agent = db.execute(
            select(Agent)
            .where(
                Agent.user_id == user_id,
                Agent.config.like(f"%{chat_id}%"),
            )
            .order_by(Agent.created_at.desc())
        ).scalars().first()
    except Exception:  # noqa: BLE001 — never fail the task on lookup
        return None
    return agent.name if agent else None


def _dm_user(db, user_id: int, text: str) -> None:
    """DM the user via the platform Telegram bot. Best-effort, never raises."""
    link = db.execute(
        select(TelegramLink).where(TelegramLink.user_id == user_id)
    ).scalar_one_or_none()
    if not link or not link.telegram_user_id:
        return
    try:
        send_message(db, str(link.telegram_user_id), text)
    except Exception:  # noqa: BLE001
        logger.exception("dm_user_failed", extra={"user_id": user_id})


def _maybe_warn_bot_not_in_group(db, schedule, response: dict | None) -> None:
    """If Telegram says we can't reach the group, DM the owner an invite link."""
    if not is_bot_not_in_chat_error(response):
        return
    bot_username = settings.telegram_bot_username
    invite = build_group_invite_link(bot_username)
    msg_lines = [
        "⚠️ I tried to send your scheduled summary but I can't read that group.",
        "Please add me to the group as an admin so I can keep monitoring it.",
    ]
    if invite:
        msg_lines.append(f'<a href="{invite}">➕ Tap here to add me to your group</a>')
    else:
        msg_lines.append(
            "Open your group → Settings → Administrators → Add → search for this bot."
        )
    _dm_user(db, schedule.user_id, "\n".join(msg_lines))


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
                body = generate_summary(
                    db, schedule.user_id, schedule.chat_id, schedule.timezone
                )
                agent_name = _resolve_agent_name_for_chat(
                    db, schedule.user_id, schedule.chat_id
                )
                message = branded_summary(agent_name, body, kind="Daily summary")
                response = send_message(db, schedule.chat_id, message)
                _maybe_warn_bot_not_in_group(db, schedule, response)
                schedule.last_sent_at = now_utc
                db.add(schedule)
            except Exception:
                logger.exception(
                    "send_summary_failed",
                    extra={"schedule_id": schedule.id, "user_id": schedule.user_id},
                )
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
        # S1: Retry until max_retries; on final failure mark the AgentRun
        # as 'failed' so it never gets stuck in 'running'/'pending'.
        try:
            countdown = settings.agent_task_retry_backoff_seconds * (2 ** self.request.retries)
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=settings.agent_task_max_retries,
            )
        except self.MaxRetriesExceededError:
            _mark_run_failed(run_id, f"max_retries_exceeded: {exc!s}")
            raise
        except Exception:
            # self.retry() raises Retry on success; only re-raise here
            # if it's a true terminal failure path.
            raise
    finally:
        db.close()


def _mark_run_failed(run_id: int, reason: str) -> None:
    """S1: ensure an AgentRun is finalized as failed even if the worker
    chain dies. Idempotent: only flips runs still in pending/running."""
    db = SessionLocal()
    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
        if not run or run.status in {"completed", "failed"}:
            return
        run.status = "failed"
        run.error_message = reason[:2000]
        run.finished_at = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        db.add(run)
        # S5: audit row so failed runs are visible alongside other events.
        try:
            from app.services.audit_log import record_audit

            record_audit(
                db,
                user_id=run.user_id,
                action="agent_run_failed",
                resource_type="agent_run",
                resource_id=str(run.id),
                metadata={
                    "agent_id": run.agent_id,
                    "reason": reason[:500],
                },
            )
        except Exception:
            logger.exception("agent_run_failed_audit_error", extra={"run_id": run_id})
        db.commit()
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
        # S1: Retry with backoff; on final exhaustion, re-raise so the
        # caller (agent_executor._execute_via_worker) propagates the
        # error up to AgentRuntime, which finalizes the run as 'failed'.
        try:
            countdown = settings.agent_task_retry_backoff_seconds * (2 ** self.request.retries)
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=settings.agent_task_max_retries,
            )
        except self.MaxRetriesExceededError:
            logger.error(
                "tool_task_exhausted",
                extra={"tool": tool_name, "user_id": internal_user_id},
            )
            raise exc
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


# Phase 6: Workflow engine async dispatch. Synchronous engine call inside
# the worker; per-step retries are handled by the engine itself.
@celery_app.task(
    name="app.worker.tasks.run_workflow_task",
    bind=True,
    soft_time_limit=settings.agent_timeout_seconds,
    time_limit=settings.agent_timeout_seconds + 30,
)
def run_workflow_task(
    self,
    user_id: int,
    agent_id: int | None,
    steps: list,
    inputs: dict | None = None,
):
    from app.services.workflow_engine import workflow_engine

    db = SessionLocal()
    try:
        result = workflow_engine.run(
            db,
            user_id=int(user_id),
            agent_id=agent_id,
            steps=list(steps),
            inputs=dict(inputs or {}),
        )
        db.commit()
        return result.to_dict()
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
        now = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        cutoff = now - timedelta(seconds=settings.agent_run_stuck_seconds)

        stuck_runs = (
            db.query(AgentRun)
            .filter(AgentRun.status == "running", AgentRun.updated_at < cutoff)
            .all()
        )
        # S1: also catch runs whose started_at exceeds AGENT_TIMEOUT_SECONDS
        # even if updated_at is fresh (e.g. a tool stuck mid-call ticking
        # heartbeats but never returning).
        timeout_cutoff = now - timedelta(seconds=settings.agent_timeout_seconds * 2)
        timed_out = (
            db.query(AgentRun)
            .filter(
                AgentRun.status.in_(["running", "pending"]),
                AgentRun.started_at.isnot(None),
                AgentRun.started_at < timeout_cutoff,
            )
            .all()
        )
        for run in timed_out:
            if run in stuck_runs:
                continue
            stuck_runs.append(run)

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
                run.finished_at = now
                db.add(run)
                db.commit()
        return {"checked": len(stuck_runs)}
    finally:
        db.close()
