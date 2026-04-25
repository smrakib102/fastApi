from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app.core.config import settings

celery_app = Celery(
    "agent_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.task_routes = {
    "app.worker.tasks.run_agent_task": {"queue": "planner"},
    "app.worker.tasks.execute_tool_task": {"queue": "tool_calls"},
    "app.worker.tasks.run_workflow_task": {"queue": "planner"},
    "app.worker.tasks.approval_wait_task": {"queue": "approvals"},
    "app.worker.tasks.send_summaries": {"queue": "long_jobs"},
    "app.worker.tasks.record_worker_heartbeat": {"queue": "default"},
    "app.worker.tasks.monitor_stuck_runs": {"queue": "default"},
    "app.worker.tasks.ping": {"queue": "default"},
}

celery_app.conf.task_queues = (
    Queue("default"),
    Queue("planner"),
    Queue("tool_calls"),
    Queue("long_jobs"),
    Queue("approvals"),
)

celery_app.conf.beat_schedule = {
    "summary-schedules": {
        "task": "app.worker.tasks.send_summaries",
        "schedule": crontab(minute="*/5"),
    },
    "planner-heartbeat": {
        "task": "app.worker.tasks.record_worker_heartbeat",
        "schedule": timedelta(seconds=settings.worker_heartbeat_seconds),
        "args": ["planner"],
        "options": {"queue": "planner"},
    },
    "tool-heartbeat": {
        "task": "app.worker.tasks.record_worker_heartbeat",
        "schedule": timedelta(seconds=settings.worker_heartbeat_seconds),
        "args": ["tool_calls"],
        "options": {"queue": "tool_calls"},
    },
    "long-heartbeat": {
        "task": "app.worker.tasks.record_worker_heartbeat",
        "schedule": timedelta(seconds=settings.worker_heartbeat_seconds),
        "args": ["long_jobs"],
        "options": {"queue": "long_jobs"},
    },
    "approvals-heartbeat": {
        "task": "app.worker.tasks.record_worker_heartbeat",
        "schedule": timedelta(seconds=settings.worker_heartbeat_seconds),
        "args": ["approvals"],
        "options": {"queue": "approvals"},
    },
    "monitor-stuck-runs": {
        "task": "app.worker.tasks.monitor_stuck_runs",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": "planner"},
    },
}
