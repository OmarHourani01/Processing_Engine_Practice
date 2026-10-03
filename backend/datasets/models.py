import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from django.db.models import Q


class Dataset(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        UPLOADING = "uploading", "Uploading"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class UploadMode(models.TextChoices):
        FOLDER = "folder", "Folder"
        ARCHIVE = "archive", "Archive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="image_datasets")
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED, db_index=True)
    upload_mode = models.CharField(max_length=16, choices=UploadMode.choices, default=UploadMode.FOLDER)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "name"]

    def __str__(self):
        return self.name


class UploadAsset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="uploads")
    relative_path = models.CharField(max_length=1024)
    object_key = models.CharField(max_length=1024, unique=True)
    expected_size = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=128)
    is_archive = models.BooleanField(default=False)
    confirmed = models.BooleanField(default=False, db_index=True)
    upload_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["dataset", "relative_path"], name="unique_dataset_upload_path"),
        ]


class StoredObjectDeletion(models.Model):
    """Durable outbox for object-store deletions after database changes commit."""

    object_key = models.CharField(max_length=1024, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]


class Image(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="images")
    relative_path = models.CharField(max_length=1024)
    object_key = models.CharField(max_length=1024, unique=True)
    thumbnail_key = models.CharField(max_length=1024, blank=True)
    preview_key = models.CharField(max_length=1024, blank=True)
    content_type = models.CharField(max_length=128)
    size = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    captured_at = models.DateTimeField(null=True, blank=True, db_index=True)
    camera_make = models.CharField(max_length=160, blank=True)
    camera_model = models.CharField(max_length=160, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    location = gis_models.PointField(srid=4326, geography=True, null=True, blank=True, spatial_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["relative_path", "id"]
        constraints = [
            models.UniqueConstraint(fields=["dataset", "relative_path"], name="unique_dataset_image_path"),
        ]
        indexes = [models.Index(fields=["dataset", "status"], name="image_dataset_status_idx")]


class Job(models.Model):
    class Kind(models.TextChoices):
        INGEST_DATASET = "ingest_dataset", "Ingest dataset"
        EXTRACT_METADATA = "extract_metadata", "Extract metadata"
        GENERATE_THUMBNAIL = "generate_thumbnail", "Generate thumbnail"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        SUCCEEDED_WITH_ERRORS = "succeeded_with_errors", "Succeeded with errors"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    ACTIVE_STATUSES = [Status.QUEUED, Status.RUNNING]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="jobs")
    image = models.ForeignKey(Image, null=True, blank=True, on_delete=models.CASCADE, related_name="jobs")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED, db_index=True)
    total_count = models.PositiveIntegerField(default=0)
    completed_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    attempt_count = models.PositiveIntegerField(default=0)
    input_data = models.JSONField(default=dict, blank=True)
    result_data = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["dataset", "status"], name="job_dataset_status_idx"),
            models.Index(fields=["parent", "status"], name="job_parent_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["image", "kind"],
                condition=Q(image__isnull=False) & Q(status__in=["queued", "running"]),
                name="one_active_image_job_per_kind",
            )
        ]


class LocalScalingPolicy(models.Model):
    """Persistent, machine-wide worker limits for the local Compose stack."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    replica_autoscaling_enabled = models.BooleanField(default=True)
    ingest_min_processes = models.PositiveSmallIntegerField(default=1)
    ingest_max_processes = models.PositiveSmallIntegerField(default=2)
    ingest_min_replicas = models.PositiveSmallIntegerField(default=1)
    ingest_max_replicas = models.PositiveSmallIntegerField(default=2)
    images_min_processes = models.PositiveSmallIntegerField(default=1)
    images_max_processes = models.PositiveSmallIntegerField(default=2)
    images_min_replicas = models.PositiveSmallIntegerField(default=1)
    images_max_replicas = models.PositiveSmallIntegerField(default=2)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        defaults = {
            "ingest_max_processes": 2,
            "ingest_max_replicas": 2 if settings.LOCAL_SCALING_SLOT_BUDGET >= 8 else 1,
            "images_max_processes": 2,
            "images_max_replicas": 2 if settings.LOCAL_SCALING_SLOT_BUDGET >= 8 else 1,
        }
        policy, _ = cls.objects.get_or_create(pk=1, defaults=defaults)
        return policy

    def as_dict(self):
        return {
            "replica_autoscaling_enabled": self.replica_autoscaling_enabled,
            "queues": {
                "ingest": {
                    "min_processes": self.ingest_min_processes,
                    "max_processes": self.ingest_max_processes,
                    "min_replicas": self.ingest_min_replicas,
                    "max_replicas": self.ingest_max_replicas,
                },
                "images": {
                    "min_processes": self.images_min_processes,
                    "max_processes": self.images_max_processes,
                    "min_replicas": self.images_min_replicas,
                    "max_replicas": self.images_max_replicas,
                },
            },
            "slot_budget": settings.LOCAL_SCALING_SLOT_BUDGET,
        }


class LocalScalingStatus(models.Model):
    """Latest controller heartbeat and queue telemetry for the local UI."""

    class State(models.TextChoices):
        HEALTHY = "healthy", "Healthy"
        DEGRADED = "degraded", "Degraded"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    state = models.CharField(max_length=16, choices=State.choices, default=State.DEGRADED)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    queues = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        status, _ = cls.objects.get_or_create(pk=1)
        return status
