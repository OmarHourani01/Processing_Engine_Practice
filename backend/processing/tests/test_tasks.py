from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from datasets.models import Dataset, Image, Job, StoredObjectDeletion, UploadAsset
from processing.exceptions import InvalidImage, RetryableProcessingError
from processing.exceptions import MissingObject
from processing.tasks import (
    _execute,
    _import_archive_asset,
    _ingest_operation,
    _mark_succeeded,
    _refresh_parent,
    _schedule_unreferenced_archive_objects,
    cleanup_stored_objects,
)


class IngestLifecycleTests(SimpleTestCase):
    def test_ingest_parent_stays_running_until_children_finish(self):
        job = SimpleNamespace(kind=Job.Kind.INGEST_DATASET)
        operation = Mock(return_value={"images_created": 1})
        operation.expected_kind = Job.Kind.INGEST_DATASET
        task = SimpleNamespace(request=SimpleNamespace(retries=0))
        job_query = Mock()
        job_query.get.return_value = SimpleNamespace(status=Job.Status.RUNNING)

        with (
            patch("processing.tasks._claim_job", return_value=job),
            patch("processing.tasks._mark_succeeded") as mark_succeeded,
            patch.object(Job.objects, "only", return_value=job_query),
        ):
            result = _execute(task, "parent-id", operation)

        self.assertEqual(result["status"], Job.Status.RUNNING)
        mark_succeeded.assert_not_called()


class TerminalImageErrorTests(SimpleTestCase):
    def test_invalid_image_is_failed_without_retry(self):
        job = SimpleNamespace(kind=Job.Kind.EXTRACT_METADATA)
        operation = Mock(side_effect=InvalidImage("The file is not a valid supported image"))
        operation.expected_kind = Job.Kind.EXTRACT_METADATA
        task = SimpleNamespace(request=SimpleNamespace(retries=0), retry=Mock())

        with (
            patch("processing.tasks._claim_job", return_value=job),
            patch("processing.tasks._set_failed") as set_failed,
        ):
            result = _execute(task, "child-id", operation)

        self.assertEqual(result["status"], Job.Status.FAILED)
        set_failed.assert_called_once()
        task.retry.assert_not_called()


class TransientRetryTests(SimpleTestCase):
    def test_transient_error_uses_exponential_retry_with_jitter(self):
        error = RetryableProcessingError("temporary storage failure")
        job = SimpleNamespace(kind=Job.Kind.EXTRACT_METADATA)
        operation = Mock(side_effect=error)
        operation.expected_kind = Job.Kind.EXTRACT_METADATA
        retry_signal = RuntimeError("celery retry")
        task = SimpleNamespace(request=SimpleNamespace(retries=0), retry=Mock(side_effect=retry_signal))

        with (
            patch("processing.tasks._claim_job", return_value=job),
            patch("processing.tasks._mark_retrying") as mark_retrying,
            patch("processing.tasks.random.uniform", return_value=0.25),
            self.assertRaisesRegex(RuntimeError, "celery retry"),
        ):
            _execute(task, "child-id", operation)

        mark_retrying.assert_called_once_with("child-id", error)
        task.retry.assert_called_once_with(exc=error, countdown=1.25, max_retries=3)


class ParentProgressTests(TestCase):
    def test_parent_progress_waits_for_both_image_jobs_and_summarizes_failures(self):
        user = get_user_model().objects.create_user(username="progress-user", password="strong-pass-123")
        dataset = Dataset.objects.create(owner=user, name="Progress dataset", status=Dataset.Status.PROCESSING)
        parent = Job.objects.create(
            dataset=dataset,
            kind=Job.Kind.INGEST_DATASET,
            status=Job.Status.RUNNING,
            total_count=2,
            result_data={"images_created": 2, "ingest_failed_count": 0},
        )
        image = Image.objects.create(
            dataset=dataset,
            relative_path="one.jpg",
            object_key="progress/one.jpg",
            content_type="image/jpeg",
            size=10,
        )
        second_image = Image.objects.create(
            dataset=dataset,
            relative_path="two.jpg",
            object_key="progress/two.jpg",
            content_type="image/jpeg",
            size=10,
        )
        for item, metadata_status, thumbnail_status in (
            (image, Job.Status.SUCCEEDED, Job.Status.SUCCEEDED),
            (second_image, Job.Status.FAILED, Job.Status.RUNNING),
        ):
            Job.objects.create(
                dataset=dataset, image=item, parent=parent, kind=Job.Kind.EXTRACT_METADATA,
                status=metadata_status,
            )
            Job.objects.create(
                dataset=dataset, image=item, parent=parent, kind=Job.Kind.GENERATE_THUMBNAIL,
                status=thumbnail_status,
            )

        with CaptureQueriesContext(connection) as queries:
            _refresh_parent(parent.pk)
        self.assertLessEqual(len(queries), 4)
        parent.refresh_from_db()
        self.assertEqual(parent.status, Job.Status.RUNNING)
        self.assertEqual(parent.completed_count, 1)
        self.assertEqual(parent.failed_count, 0)

        Job.objects.filter(image=second_image, kind=Job.Kind.GENERATE_THUMBNAIL).update(status=Job.Status.FAILED)
        with transaction.atomic():
            _refresh_parent(parent.pk)
        parent.refresh_from_db()
        dataset.refresh_from_db()
        self.assertEqual(parent.status, Job.Status.SUCCEEDED_WITH_ERRORS)
        self.assertEqual(parent.completed_count, 1)
        self.assertEqual(parent.failed_count, 1)
        self.assertEqual(parent.result_data["images_failed"], 1)
        self.assertEqual(dataset.status, Dataset.Status.COMPLETED)


class CancellationFenceTests(TestCase):
    @patch("processing.tasks.stat_object", return_value={"ContentLength": 10})
    def test_cancelled_ingest_does_not_restore_processing_dataset_state(self, _stat_object):
        user = get_user_model().objects.create_user(username="cancel-ingest-user", password="strong-pass-123")
        dataset = Dataset.objects.create(owner=user, name="Cancelled ingest")
        parent = Job.objects.create(
            dataset=dataset,
            kind=Job.Kind.INGEST_DATASET,
            status=Job.Status.CANCELLED,
            input_data={"upload_asset_ids": []},
        )
        asset = UploadAsset.objects.create(
            dataset=dataset,
            relative_path="photo.jpg",
            object_key="cancel-ingest/photo.jpg",
            expected_size=10,
            content_type="image/jpeg",
            confirmed=True,
        )
        parent.input_data = {"upload_asset_ids": [str(asset.pk)]}
        parent.save(update_fields=["input_data"])

        result = _ingest_operation(parent)

        dataset.refresh_from_db()
        self.assertTrue(result["cancelled"])
        self.assertEqual(dataset.status, Dataset.Status.CREATED)


    def test_cancelled_parent_prevents_late_child_results_from_being_saved(self):
        user = get_user_model().objects.create_user(username="cancel-user", password="strong-pass-123")
        dataset = Dataset.objects.create(owner=user, name="Cancelled dataset")
        parent = Job.objects.create(dataset=dataset, kind=Job.Kind.INGEST_DATASET, status=Job.Status.CANCELLED)
        image = Image.objects.create(
            dataset=dataset,
            relative_path="late.jpg",
            object_key="cancel/late.jpg",
            content_type="image/jpeg",
            size=10,
        )
        child = Job.objects.create(
            dataset=dataset,
            image=image,
            parent=parent,
            kind=Job.Kind.EXTRACT_METADATA,
            status=Job.Status.RUNNING,
        )

        self.assertFalse(_mark_succeeded(child.pk, {
            "width": 64,
            "height": 32,
            "captured_at": None,
            "camera_make": "Camera",
            "camera_model": "Model",
            "gps": [35.0, 32.0],
        }))
        image.refresh_from_db()
        child.refresh_from_db()
        self.assertIsNone(image.width)
        self.assertIsNone(image.location)
        self.assertEqual(child.status, Job.Status.CANCELLED)


class ArchiveObjectCleanupTests(TestCase):
    def test_archive_import_tracks_keys_before_a_partial_storage_failure(self):
        asset = SimpleNamespace(object_key="uploads/archive.zip")
        entries = [
            SimpleNamespace(
                relative_path=f"photo-{index}.jpg",
                object_key=f"datasets/archive/photo-{index}.jpg",
                content_type="image/jpeg",
                size=1,
                data=BytesIO(b"x"),
            )
            for index in (1, 2)
        ]
        object_keys = []
        with (
            patch("processing.tasks.iter_zip_images", return_value=iter(entries)),
            patch("processing.tasks.stat_object", side_effect=MissingObject("missing")),
            patch("processing.tasks.upload_file", side_effect=[None, RuntimeError("partial write")]),
        ):
            with self.assertRaisesRegex(RuntimeError, "partial write"):
                _import_archive_asset(asset, "00000000-0000-0000-0000-000000000001", BytesIO(), object_keys)

        self.assertEqual(object_keys, [entry.object_key for entry in entries])

    @patch("processing.tasks.schedule_object_deletions")
    def test_archive_cleanup_preserves_keys_already_used_by_images(self, schedule_deletions):
        user = get_user_model().objects.create_user(username="archive-clean-user", password="strong-pass-123")
        dataset = Dataset.objects.create(owner=user, name="Archive cleanup")
        Image.objects.create(
            dataset=dataset,
            relative_path="kept.jpg",
            object_key="datasets/archive/kept.jpg",
            content_type="image/jpeg",
            size=1,
        )

        _schedule_unreferenced_archive_objects([
            "datasets/archive/kept.jpg",
            "datasets/archive/orphan.jpg",
        ])

        schedule_deletions.assert_called_once_with({"datasets/archive/orphan.jpg"})


class StoredObjectCleanupTaskTests(TestCase):
    @patch("processing.tasks.delete_objects")
    def test_successful_cleanup_removes_outbox_rows(self, delete_objects_mock):
        row = StoredObjectDeletion.objects.create(object_key="unused/preview.webp")

        result = cleanup_stored_objects.run()

        self.assertEqual(result, {"deleted": 1})
        delete_objects_mock.assert_called_once_with(["unused/preview.webp"])
        self.assertFalse(StoredObjectDeletion.objects.filter(pk=row.pk).exists())
