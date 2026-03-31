from celery import Celery
from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "frontdesk_ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # process one task at a time per worker
    task_routes={
        "app.workers.tasks.process_inbound_message": {"queue": "messages"},
        "app.workers.tasks.send_escalation_notification": {"queue": "notifications"},
    },
)
