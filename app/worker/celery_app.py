from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app.core.config import settings

celery_app = Celery(
    "agent_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.task_routes = {
    "app.worker.tasks.run_agent_task": {"queue": "agent_runs"},
    "app.worker.tasks.execute_tool_task": {"queue": "tool_calls"},
    "app.worker.tasks.approval_wait_task": {"queue": "approvals"},
    "app.worker.tasks.send_summaries": {"queue": "default"},
    "app.worker.tasks.ping": {"queue": "default"},
}

celery_app.conf.task_queues = (
    Queue("default"),
    Queue("agent_runs"),
    Queue("tool_calls"),
    Queue("approvals"),
)

celery_app.conf.beat_schedule = {
    "summary-schedules": {
        "task": "app.worker.tasks.send_summaries",
        "schedule": crontab(minute="*/5"),
    }
}
