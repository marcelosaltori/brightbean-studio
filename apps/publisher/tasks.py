"""Background tasks for the publishing engine."""

import logging

from background_task import background
from django.utils import timezone

logger = logging.getLogger(__name__)


@background(schedule=0)
def run_publish_cycle():
    """Poll for due posts and publish them.

    Registered as a recurring task (every 15s) so that
    ``python manage.py process_tasks`` handles publishing
    without needing a separate ``run_publisher`` process.
    """
    from apps.publisher.engine import PublishEngine
    from apps.publisher.models import WorkerHeartbeat

    heartbeat, _ = WorkerHeartbeat.objects.get_or_create(name="publisher")
    heartbeat.last_started_at = timezone.now()
    heartbeat.save(update_fields=["last_started_at"])

    try:
        published = PublishEngine().poll_and_publish()
    except Exception as exc:
        heartbeat.last_error_at = timezone.now()
        heartbeat.last_error_class = type(exc).__name__[:255]
        heartbeat.save(update_fields=["last_error_at", "last_error_class"])
        raise

    heartbeat.last_completed_at = timezone.now()
    heartbeat.last_error_class = ""
    heartbeat.save(update_fields=["last_completed_at", "last_error_class"])
    if published:
        logger.info("Publish cycle completed - %d post(s) published", published)
