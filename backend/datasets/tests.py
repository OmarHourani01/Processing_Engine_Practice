from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Dataset, Image, Job, StoredObjectDeletion, UploadAsset

User = get_user_model()


class AuthAndOwnershipTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="strong-pass-123")
        self.other = User.objects.create_user(username="other", password="strong-pass-123")
        self.dataset = Dataset.objects.create(owner=self.owner, name="Field trip")
        self.image = Image.objects.create(
            dataset=self.dataset,
            relative_path="day-1/photo.jpg",
            object_key="owner/photo.jpg",
            content_type="image/jpeg",
            size=10,
            location=Point(35.91, 31.95, srid=4326),
        )
        self.client = APIClient()

    def test_anonymous_client_cannot_list_datasets(self):
        response = self.client.get("/api/datasets/")
        self.assertIn(response.status_code, (401, 403))

    def test_dataset_and_image_are_scoped_to_owner(self):
        self.client.force_authenticate(self.other)
        dataset_response = self.client.get(f"/api/datasets/{self.dataset.pk}/")
        image_response = self.client.get(f"/api/images/{self.image.pk}/")
        self.assertEqual(dataset_response.status_code, 404)
        self.assertEqual(image_response.status_code, 404)

    def test_list_and_geojson_only_include_current_owners_dataset(self):
        Dataset.objects.create(owner=self.other, name="Other data")
        self.client.force_authenticate(self.owner)
        datasets = self.client.get("/api/datasets/")
        map_response = self.client.get(f"/api/datasets/{self.dataset.pk}/map/")
        self.assertEqual(datasets.status_code, 200)
        self.assertEqual(datasets.data["count"], 1)
        self.assertEqual(map_response.data["type"], "FeatureCollection")
        self.assertEqual(len(map_response.data["features"]), 1)
        self.assertEqual(map_response.data["features"][0]["geometry"]["coordinates"], [35.91, 31.95])

    def test_csrf_endpoint_returns_token_and_sets_cookie(self):
        response = self.client.get("/api/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["csrfToken"])
        self.assertIn("csrftoken", response.cookies)

    def test_registration_requires_csrf_when_checks_are_enabled(self):
        client = APIClient(enforce_csrf_checks=True)
        response = client.post("/api/auth/register/", {"username": "fresh", "password": "Strong-pass-456!"}, format="json")
        self.assertEqual(response.status_code, 403)
        token = client.get("/api/auth/csrf/").data["csrfToken"]
        response = client.post(
            "/api/auth/register/",
            {"username": "fresh", "password": "Strong-pass-456!"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["user"]["username"], "fresh")


class DatasetUploadAndJobApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="user", password="strong-pass-123")
        self.dataset = Dataset.objects.create(owner=self.user, name="Survey")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("datasets.views.ensure_bucket_and_cors")
    @patch("datasets.views.presigned_upload")
    def test_upload_prepare_returns_form_post_and_rejects_unsafe_paths(self, presign, ensure):
        presign.return_value = {"url": "http://localhost:9000/image-datasets", "fields": {"key": "signed"}}
        unsafe = self.client.post(
            f"/api/datasets/{self.dataset.pk}/uploads/prepare/",
            {"files": [{"name": "../escape.jpg", "size": 20, "content_type": "image/jpeg"}]},
            format="json",
        )
        self.assertEqual(unsafe.status_code, 400)
        ensure.assert_not_called()

        duplicate = self.client.post(
            f"/api/datasets/{self.dataset.pk}/uploads/prepare/",
            {"files": [
                {"name": "trip/duplicate.jpg", "size": 20},
                {"name": "trip/duplicate.jpg", "size": 20},
            ]},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 400)
        ensure.assert_not_called()

        response = self.client.post(
            f"/api/datasets/{self.dataset.pk}/uploads/prepare/",
            {"files": [{"name": "trip/one.jpg", "size": 20, "content_type": "image/jpeg"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["uploads"][0]["method"], "POST")
        self.assertEqual(response.data["uploads"][0]["fields"], {"key": "signed"})
        self.assertTrue(response.data["uploads"][0]["id"])
        ensure.assert_called_once()

    @patch("datasets.views.schedule_object_deletions")
    @patch("datasets.views.ensure_bucket_and_cors")
    @patch("datasets.views.presigned_upload", return_value={"url": "http://localhost/upload", "fields": {}})
    def test_retry_reuses_failed_path_and_does_not_double_count_quota(self, presign, ensure, schedule_deletions):
        old = UploadAsset.objects.create(
            dataset=self.dataset,
            relative_path="trip/retry.jpg",
            object_key="users/1/old/retry.jpg",
            expected_size=40,
            content_type="image/jpeg",
            upload_error="Object not found",
        )
        with override_settings(UPLOAD_MAX_BYTES=50):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    f"/api/datasets/{self.dataset.pk}/uploads/prepare/",
                    {"files": [{"name": "trip/retry.jpg", "size": 40, "content_type": "image/jpeg"}]},
                    format="json",
                )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["uploads"][0]["id"], str(old.pk))
        self.assertEqual(UploadAsset.objects.filter(dataset=self.dataset).count(), 1)
        old.refresh_from_db()
        self.assertEqual(old.expected_size, 40)
        self.assertFalse(old.confirmed)
        self.assertEqual(old.upload_error, "")
        self.assertNotEqual(old.object_key, "users/1/old/retry.jpg")
        schedule_deletions.assert_called_once_with(["users/1/old/retry.jpg"])

    @override_settings(UPLOAD_MAX_BYTES=50)
    def test_prepare_rejects_aggregate_size_over_dataset_limit(self):
        UploadAsset.objects.create(
            dataset=self.dataset,
            relative_path="existing.jpg",
            object_key="users/1/existing.jpg",
            expected_size=40,
            content_type="image/jpeg",
        )
        response = self.client.post(
            f"/api/datasets/{self.dataset.pk}/uploads/prepare/",
            {"files": [{"name": "new.jpg", "size": 20, "content_type": "image/jpeg"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(UploadAsset.objects.filter(dataset=self.dataset).count(), 1)

    @patch("datasets.views.object_size", return_value=20)
    def test_confirmation_and_process_create_durable_parent_job(self, object_size_mock):
        asset = UploadAsset.objects.create(
            dataset=self.dataset,
            relative_path="trip/one.jpg",
            object_key="users/1/one.jpg",
            expected_size=20,
            content_type="image/jpeg",
        )
        confirmed = self.client.post(
            f"/api/datasets/{self.dataset.pk}/uploads/confirm/",
            {"upload_ids": [str(asset.pk)]},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.data["uploads"][0]["confirmed"])

        with patch("datasets.views.enqueue_job") as enqueue, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/datasets/{self.dataset.pk}/process/", {}, format="json")
        self.assertEqual(response.status_code, 202)
        job = Job.objects.get(pk=response.data["job"]["id"])
        self.assertEqual(job.kind, Job.Kind.INGEST_DATASET)
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.total_count, 1)
        enqueue.assert_called_once_with(job.pk, Job.Kind.INGEST_DATASET)
        object_size_mock.assert_called_once()

    def test_partial_processing_requires_explicit_opt_in(self):
        UploadAsset.objects.create(
            dataset=self.dataset,
            relative_path="trip/failed.jpg",
            object_key="users/1/failed.jpg",
            expected_size=20,
            content_type="image/jpeg",
            upload_error="Object not found",
        )
        response = self.client.post(f"/api/datasets/{self.dataset.pk}/process/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "uploads_incomplete")

    @patch("datasets.views.enqueue_job")
    def test_partial_process_records_failed_paths_omitted_from_selected_ids(self, enqueue):
        confirmed = UploadAsset.objects.create(
            dataset=self.dataset,
            relative_path="trip/good.jpg",
            object_key="users/1/good.jpg",
            expected_size=20,
            content_type="image/jpeg",
            confirmed=True,
        )
        failed = UploadAsset.objects.create(
            dataset=self.dataset,
            relative_path="trip/bad.jpg",
            object_key="users/1/bad.jpg",
            expected_size=20,
            content_type="image/jpeg",
            upload_error="Upload connection failed",
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/datasets/{self.dataset.pk}/process/",
                {"upload_ids": [str(confirmed.pk)], "allow_partial": True},
                format="json",
            )
        self.assertEqual(response.status_code, 202)
        job = Job.objects.get(pk=response.data["job"]["id"])
        self.assertEqual(job.input_data["upload_asset_ids"], [str(confirmed.pk)])
        self.assertEqual(job.errors[0]["upload_id"], str(failed.pk))
        enqueue.assert_called_once()

    @patch("datasets.views.enqueue_job")
    def test_manual_image_job_returns_id_and_active_duplicate_is_idempotent(self, enqueue):
        image = Image.objects.create(
            dataset=self.dataset,
            relative_path="one.jpg",
            object_key="users/1/one.jpg",
            content_type="image/jpeg",
            size=10,
        )
        payload = {"kind": Job.Kind.EXTRACT_METADATA, "image_id": str(image.pk)}
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post("/api/jobs/", payload, format="json")
            second = self.client.post("/api/jobs/", payload, format="json")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.data["job"]["id"], second.data["job"]["id"])
        self.assertEqual(Job.objects.filter(image=image, kind=Job.Kind.EXTRACT_METADATA).count(), 1)
        enqueue.assert_called_once()

    def test_jobs_can_be_filtered_by_kind_status_dataset_and_parent(self):
        parent = Job.objects.create(dataset=self.dataset, kind=Job.Kind.INGEST_DATASET)
        image = Image.objects.create(
            dataset=self.dataset,
            relative_path="problem/invalid.tif",
            object_key="users/1/problem/invalid.tif",
            content_type="image/tiff",
            size=10,
        )
        child = Job.objects.create(
            dataset=self.dataset,
            image=image,
            parent=parent,
            kind=Job.Kind.EXTRACT_METADATA,
            status=Job.Status.FAILED,
            failed_count=1,
            errors=[{"error": "bad image"}],
        )
        response = self.client.get(
            f"/api/jobs/?type=extract_metadata&status=failed&dataset={self.dataset.pk}&parent_job={parent.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(child.pk))
        self.assertEqual(response.data["results"][0]["failed_count"], 1)
        self.assertEqual(response.data["results"][0]["image_path"], "problem/invalid.tif")

    def test_job_list_does_not_expose_another_users_jobs(self):
        foreign = User.objects.create_user(username="foreign", password="strong-pass-123")
        other_dataset = Dataset.objects.create(owner=foreign, name="Private")
        Job.objects.create(dataset=other_dataset, kind=Job.Kind.INGEST_DATASET)
        own_job = Job.objects.create(dataset=self.dataset, kind=Job.Kind.INGEST_DATASET)
        response = self.client.get("/api/jobs/")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(own_job.pk))

    def test_completed_job_detail_returns_status_and_result(self):
        job = Job.objects.create(
            dataset=self.dataset,
            kind=Job.Kind.EXTRACT_METADATA,
            status=Job.Status.SUCCEEDED,
            total_count=1,
            completed_count=1,
            result_data={"width": 640, "height": 480, "camera_make": "Example"},
        )

        response = self.client.get(f"/api/jobs/{job.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(job.pk))
        self.assertEqual(response.data["status"], Job.Status.SUCCEEDED)
        self.assertEqual(response.data["result"], job.result_data)

    def test_job_detail_does_not_expose_another_users_job(self):
        foreign = User.objects.create_user(username="job-owner", password="strong-pass-123")
        other_dataset = Dataset.objects.create(owner=foreign, name="Private job data")
        job = Job.objects.create(dataset=other_dataset, kind=Job.Kind.INGEST_DATASET)

        response = self.client.get(f"/api/jobs/{job.pk}/")

        self.assertEqual(response.status_code, 404)

    def test_image_list_can_filter_to_images_without_a_location_before_pagination(self):
        unlocated = Image.objects.create(
            dataset=self.dataset,
            relative_path="no-gps.jpg",
            object_key="users/1/no-gps.jpg",
            content_type="image/jpeg",
            size=10,
        )
        response = self.client.get(f"/api/datasets/{self.dataset.pk}/images/?has_location=false")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data["results"]], [str(unlocated.pk)])


class StoredObjectDeletionOutboxTests(TestCase):
    @patch("datasets.tasks_dispatch.enqueue_storage_cleanup")
    def test_schedules_unique_keys_durably_after_commit(self, enqueue):
        from datasets.storage import schedule_object_deletions

        with self.captureOnCommitCallbacks(execute=True):
            schedule_object_deletions(["one.jpg", "two.jpg", "one.jpg"])

        self.assertEqual(
            set(StoredObjectDeletion.objects.values_list("object_key", flat=True)),
            {"one.jpg", "two.jpg"},
        )
        enqueue.assert_called_once_with()
