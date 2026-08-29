from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.publisher.models import WorkerHeartbeat
from apps.publisher.tasks import run_publish_cycle


class WorkerHeartbeatTest(TestCase):
    @patch("apps.publisher.engine.PublishEngine.poll_and_publish", return_value=0)
    def test_publish_cycle_records_successful_heartbeat(self, _mock_publish):
        run_publish_cycle.now()

        heartbeat = WorkerHeartbeat.objects.get(name="publisher")
        self.assertIsNotNone(heartbeat.last_started_at)
        self.assertIsNotNone(heartbeat.last_completed_at)
        self.assertEqual(heartbeat.last_error_class, "")

        stdout = StringIO()
        call_command("check_worker_health", max_age=300, stdout=stdout)
        self.assertIn("publisher worker healthy", stdout.getvalue())

    def test_stale_heartbeat_fails_health_check(self):
        WorkerHeartbeat.objects.create(
            name="publisher",
            last_started_at=timezone.now() - timedelta(minutes=10),
            last_completed_at=timezone.now() - timedelta(minutes=10),
        )

        with self.assertRaisesMessage(CommandError, "heartbeat is stale"):
            call_command("check_worker_health", max_age=300)
