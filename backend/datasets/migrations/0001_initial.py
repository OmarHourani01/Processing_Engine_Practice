import uuid

import django.contrib.gis.db.models.fields
import django.contrib.postgres.operations
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        django.contrib.postgres.operations.CreateExtension("postgis"),
        migrations.CreateModel(
            name="Dataset",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=160)),
                ("status", models.CharField(choices=[("created", "Created"), ("uploading", "Uploading"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed")], db_index=True, default="created", max_length=20)),
                ("upload_mode", models.CharField(choices=[("folder", "Folder"), ("archive", "Archive")], default="folder", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="image_datasets", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="UploadAsset",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("relative_path", models.CharField(max_length=1024)),
                ("object_key", models.CharField(max_length=1024, unique=True)),
                ("expected_size", models.PositiveBigIntegerField()),
                ("content_type", models.CharField(max_length=128)),
                ("is_archive", models.BooleanField(default=False)),
                ("confirmed", models.BooleanField(db_index=True, default=False)),
                ("upload_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="uploads", to="datasets.dataset")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="Image",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("relative_path", models.CharField(max_length=1024)),
                ("object_key", models.CharField(max_length=1024, unique=True)),
                ("thumbnail_key", models.CharField(blank=True, max_length=1024)),
                ("preview_key", models.CharField(blank=True, max_length=1024)),
                ("content_type", models.CharField(max_length=128)),
                ("size", models.PositiveBigIntegerField()),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("processed", "Processed"), ("failed", "Failed")], db_index=True, default="pending", max_length=16)),
                ("captured_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("camera_make", models.CharField(blank=True, max_length=160)),
                ("camera_model", models.CharField(blank=True, max_length=160)),
                ("width", models.PositiveIntegerField(blank=True, null=True)),
                ("height", models.PositiveIntegerField(blank=True, null=True)),
                ("location", django.contrib.gis.db.models.fields.PointField(blank=True, geography=True, null=True, spatial_index=True, srid=4326)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="images", to="datasets.dataset")),
            ],
            options={"ordering": ["relative_path", "id"]},
        ),
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("ingest_dataset", "Ingest dataset"), ("extract_metadata", "Extract metadata"), ("generate_thumbnail", "Generate thumbnail")], db_index=True, max_length=32)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("succeeded", "Succeeded"), ("succeeded_with_errors", "Succeeded with errors"), ("failed", "Failed")], db_index=True, default="queued", max_length=24)),
                ("total_count", models.PositiveIntegerField(default=0)),
                ("completed_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("input_data", models.JSONField(blank=True, default=dict)),
                ("result_data", models.JSONField(blank=True, default=dict)),
                ("errors", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="jobs", to="datasets.dataset")),
                ("image", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="jobs", to="datasets.image")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="children", to="datasets.job")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="uploadasset",
            constraint=models.UniqueConstraint(fields=("dataset", "relative_path"), name="unique_dataset_upload_path"),
        ),
        migrations.AddConstraint(
            model_name="image",
            constraint=models.UniqueConstraint(fields=("dataset", "relative_path"), name="unique_dataset_image_path"),
        ),
        migrations.AddIndex(
            model_name="image",
            index=models.Index(fields=["dataset", "status"], name="image_dataset_status_idx"),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(fields=["dataset", "status"], name="job_dataset_status_idx"),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(fields=["parent", "status"], name="job_parent_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.UniqueConstraint(condition=models.Q(("image__isnull", False), ("status__in", ["queued", "running"])), fields=("image", "kind"), name="one_active_image_job_per_kind"),
        ),
    ]
