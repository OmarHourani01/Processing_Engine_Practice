import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("image_datasets")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.conf.beat_schedule = {
    "reconcile-stale-jobs-every-minute": {
        "task": "processing.tasks.reconcile_jobs",
        "schedule": 60.0,
    },
    "retry-stored-object-cleanup-every-minute": {
        "task": "processing.tasks.cleanup_stored_objects",
        "schedule": 60.0,
    },
}
