from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0020_platformpost_first_comment_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="platformpost",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("pending_review", "Pending Review"),
                    ("pending_client", "Pending Client"),
                    ("approved", "Approved"),
                    ("changes_requested", "Changes Requested"),
                    ("rejected", "Rejected"),
                    ("scheduled", "Scheduled"),
                    ("publishing", "Publishing"),
                    ("published", "Published"),
                    ("failed", "Failed"),
                    ("unknown", "Outcome unknown"),
                    ("on_hold", "On Hold"),
                ],
                db_index=True,
                default="draft",
                max_length=30,
            ),
        ),
    ]
