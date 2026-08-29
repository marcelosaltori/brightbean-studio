from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publisher", "0002_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkerHeartbeat",
            fields=[
                ("name", models.CharField(default="publisher", max_length=64, primary_key=True, serialize=False)),
                ("last_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_completed_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_class", models.CharField(blank=True, default="", max_length=255)),
            ],
            options={"db_table": "publisher_worker_heartbeat"},
        ),
    ]
