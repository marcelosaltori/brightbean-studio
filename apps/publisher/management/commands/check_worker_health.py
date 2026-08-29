from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.publisher.models import WorkerHeartbeat


class Command(BaseCommand):
    help = "Fail unless the publishing worker heartbeat is recent and its latest cycle succeeded."

    def add_arguments(self, parser):
        parser.add_argument("--max-age", type=int, default=300, help="Maximum heartbeat age in seconds.")

    def handle(self, *args, **options):
        heartbeat = WorkerHeartbeat.objects.filter(name="publisher").first()
        if heartbeat is None or heartbeat.last_started_at is None:
            raise CommandError("Publisher heartbeat has not started")

        cutoff = timezone.now() - timedelta(seconds=options["max_age"])
        if heartbeat.last_started_at < cutoff:
            raise CommandError("Publisher heartbeat is stale")

        if heartbeat.last_error_at and (
            heartbeat.last_completed_at is None or heartbeat.last_error_at > heartbeat.last_completed_at
        ):
            raise CommandError(f"Publisher heartbeat failed: {heartbeat.last_error_class or 'unknown error'}")

        self.stdout.write("publisher worker healthy")
