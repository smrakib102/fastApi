from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "agent_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.task_routes = {
    "app.worker.tasks.*": {"queue": "default"}
}

celery_app.conf.beat_schedule = {
    "summary-schedules": {
        "task": "app.worker.tasks.send_summaries",
        "schedule": crontab(minute="*/5"),
    }
}
