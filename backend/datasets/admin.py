from django.contrib import admin

from .models import Dataset, Image, Job, StoredObjectDeletion, UploadAsset


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status", "upload_mode", "created_at")
    list_filter = ("status", "upload_mode")
    search_fields = ("name", "owner__username")


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    list_display = ("relative_path", "dataset", "status", "captured_at")
    list_filter = ("status",)
    search_fields = ("relative_path", "dataset__name")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "status", "dataset", "attempt_count", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("id", "dataset__name")


admin.site.register(UploadAsset)
admin.site.register(StoredObjectDeletion)
