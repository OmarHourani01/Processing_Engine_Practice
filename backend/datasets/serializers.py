from django.db.models import Count, Q
from rest_framework import serializers

from .models import Dataset, Image, Job, UploadAsset
from .storage import signed_object_url


class DatasetSerializer(serializers.ModelSerializer):
    image_count = serializers.IntegerField(read_only=True)
    geotagged_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Dataset
        fields = ("id", "name", "status", "upload_mode", "created_at", "updated_at", "image_count", "geotagged_count")
        read_only_fields = ("id", "status", "created_at", "updated_at", "image_count", "geotagged_count")


class ImageSerializer(serializers.ModelSerializer):
    dataset_id = serializers.UUIDField(read_only=True)
    location = serializers.SerializerMethodField()
    original_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()

    class Meta:
        model = Image
        fields = (
            "id", "dataset_id", "relative_path", "content_type", "size", "status",
            "captured_at", "camera_make", "camera_model", "width", "height", "location",
            "original_url", "thumbnail_url", "preview_url", "created_at", "updated_at",
        )

    def get_location(self, obj):
        if not obj.location:
            return None
        return {"type": "Point", "coordinates": [obj.location.x, obj.location.y]}

    def get_original_url(self, obj):
        return signed_object_url(obj.object_key)

    def get_thumbnail_url(self, obj):
        return signed_object_url(obj.thumbnail_key) if obj.thumbnail_key else None

    def get_preview_url(self, obj):
        return signed_object_url(obj.preview_key) if obj.preview_key else None


class JobSerializer(serializers.ModelSerializer):
    dataset_id = serializers.UUIDField(read_only=True)
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)
    parent_id = serializers.UUIDField(read_only=True)
    image_id = serializers.UUIDField(read_only=True)
    image_path = serializers.CharField(source="image.relative_path", read_only=True, allow_null=True)
    input = serializers.JSONField(source="input_data", read_only=True)
    result = serializers.JSONField(source="result_data", read_only=True)
    children_url = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = (
            "id", "dataset_id", "dataset_name", "image_id", "image_path", "parent_id", "kind", "status", "total_count",
            "completed_count", "failed_count", "attempt_count", "input", "result", "errors",
            "children_url", "created_at", "updated_at", "started_at", "finished_at",
        )

    def get_children_url(self, obj):
        return f"/api/jobs/?parent_job={obj.pk}"


def owned_dataset_queryset(user):
    return Dataset.objects.filter(owner=user).annotate(
        image_count=Count("images", distinct=True),
        geotagged_count=Count("images", filter=Q(images__location__isnull=False), distinct=True),
    )


class UploadAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = UploadAsset
        fields = ("id", "relative_path", "expected_size", "content_type", "is_archive", "confirmed", "upload_error")
