from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("datasets", "0002_job_cancelled_status")]

    operations = [
        migrations.CreateModel(
            name="LocalScalingPolicy",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("replica_autoscaling_enabled", models.BooleanField(default=True)),
                ("ingest_min_processes", models.PositiveSmallIntegerField(default=1)),
                ("ingest_max_processes", models.PositiveSmallIntegerField(default=2)),
                ("ingest_min_replicas", models.PositiveSmallIntegerField(default=1)),
                ("ingest_max_replicas", models.PositiveSmallIntegerField(default=2)),
                ("images_min_processes", models.PositiveSmallIntegerField(default=1)),
                ("images_max_processes", models.PositiveSmallIntegerField(default=2)),
                ("images_min_replicas", models.PositiveSmallIntegerField(default=1)),
                ("images_max_replicas", models.PositiveSmallIntegerField(default=2)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="LocalScalingStatus",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("state", models.CharField(choices=[("healthy", "Healthy"), ("degraded", "Degraded")], default="degraded", max_length=16)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.CharField(blank=True, max_length=500)),
                ("queues", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
