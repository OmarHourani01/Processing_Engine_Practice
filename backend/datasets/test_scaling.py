import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from datasets.models import LocalScalingPolicy, LocalScalingStatus


class LocalScalingApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="scaler", password="safe-pass-123")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.policy = {
            "replica_autoscaling_enabled": True,
            "queues": {
                "ingest": {"min_processes": 1, "max_processes": 2, "min_replicas": 1, "max_replicas": 2},
                "images": {"min_processes": 1, "max_processes": 2, "min_replicas": 1, "max_replicas": 2},
            },
        }

    @override_settings(LOCAL_SCALING_ENABLED=False)
    def test_api_is_hidden_when_local_launcher_has_not_enabled_it(self):
        response = self.client.get("/api/local/scaling/")
        self.assertEqual(response.status_code, 404)

    @override_settings(LOCAL_SCALING_ENABLED=True)
    def test_authenticated_user_can_read_and_update_machine_policy(self):
        read = self.client.get("/api/local/scaling/")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.data["status"]["state"], "offline")
        self.assertEqual(read.data["policy"]["slot_budget"], 8)

        self.policy["replica_autoscaling_enabled"] = False
        self.policy["queues"]["images"]["max_replicas"] = 1
        saved = self.client.put("/api/local/scaling/", self.policy, format="json")
        self.assertEqual(saved.status_code, 200)
        self.assertFalse(saved.data["policy"]["replica_autoscaling_enabled"])
        self.assertEqual(saved.data["policy"]["queues"]["images"]["max_replicas"], 1)
        row = LocalScalingPolicy.objects.get(pk=1)
        self.assertFalse(row.replica_autoscaling_enabled)
        self.assertEqual(row.images_max_replicas, 1)

    @override_settings(LOCAL_SCALING_ENABLED=True)
    def test_rejects_invalid_ranges_and_slot_budget_overflow(self):
        self.policy["queues"]["ingest"]["min_processes"] = 3
        invalid_range = self.client.put("/api/local/scaling/", self.policy, format="json")
        self.assertEqual(invalid_range.status_code, 400)

        self.policy["queues"]["ingest"]["min_processes"] = 1
        with override_settings(LOCAL_SCALING_SLOT_BUDGET=7):
            over_budget = self.client.put("/api/local/scaling/", self.policy, format="json")
        self.assertEqual(over_budget.status_code, 400)
        self.assertFalse(LocalScalingPolicy.objects.filter(pk=1).exists())

    @override_settings(LOCAL_SCALING_ENABLED=True)
    def test_status_is_offline_after_heartbeat_expires(self):
        LocalScalingStatus.objects.create(
            state=LocalScalingStatus.State.HEALTHY,
            last_seen_at=timezone.now() - timedelta(seconds=46),
        )
        response = self.client.get("/api/local/scaling/")
        self.assertEqual(response.data["status"]["state"], "offline")

    @override_settings(LOCAL_SCALING_ENABLED=True)
    def test_put_requires_csrf_for_session_authenticated_user(self):
        client = APIClient(enforce_csrf_checks=True)
        self.assertTrue(client.login(username="scaler", password="safe-pass-123"))
        csrf_token = client.get("/api/auth/csrf/").data["csrfToken"]
        response = client.put("/api/local/scaling/", self.policy, format="json", HTTP_X_CSRFTOKEN=csrf_token)
        self.assertEqual(response.status_code, 200)

    def test_anonymous_user_cannot_read_policy(self):
        response = APIClient().get("/api/local/scaling/")
        self.assertIn(response.status_code, (401, 403))

    @override_settings(LOCAL_SCALING_SLOT_BUDGET=6)
    def test_first_policy_defaults_fit_a_smaller_configured_budget(self):
        policy = LocalScalingPolicy.get_solo()
        total_slots = policy.ingest_max_processes * policy.ingest_max_replicas + policy.images_max_processes * policy.images_max_replicas
        self.assertLessEqual(total_slots, 6)


class LocalScalingCommandTests(TestCase):
    def test_recognizes_celery_autoscale_acknowledgement(self):
        from datasets.management.commands.local_scaling_status import _acknowledged_workers

        replies = [{"ingest@worker-one": {"ok": "autoscale now max=2 min=1"}}]
        self.assertEqual(_acknowledged_workers(replies), {"ingest@worker-one"})

    @patch("datasets.management.commands.local_scaling_status.Command._inspect", side_effect=RuntimeError("Redis unavailable"))
    def test_failed_worker_inspection_records_degraded_status(self, inspect):
        output = StringIO()
        call_command("local_scaling_status", ingest_replicas=1, images_replicas=1, stdout=output)
        status = LocalScalingStatus.objects.get(pk=1)
        self.assertEqual(status.state, LocalScalingStatus.State.DEGRADED)
        self.assertIn("Redis unavailable", status.error)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    @patch("datasets.management.commands.local_scaling_status.Redis.from_url")
    @patch("datasets.management.commands.local_scaling_status.celery_app.control.autoscale")
    @patch("datasets.management.commands.local_scaling_status.celery_app.control.inspect")
    def test_inspection_failure_does_not_change_any_worker_process_limits(self, inspect, autoscale, from_url):
        inspector = inspect.return_value
        inspector.active.return_value = {"ingest@one": [], "images@one": []}
        inspector.reserved.return_value = {"ingest@one": [], "images@one": []}
        # The second queue is missing from this response, so inspection must
        # fail before applying process limits to the already inspected queue.
        inspector.scheduled.return_value = {"ingest@one": [], "images@one": []}
        inspector.stats.return_value = {
            "ingest@one": {"pool": {"processes": [123]}},
            "images@one": {"pool": {"processes": [456]}},
        }
        inspector.active.return_value = {"ingest@one": []}
        redis = from_url.return_value
        redis.llen.return_value = 0

        from datasets.management.commands.local_scaling_status import Command, ScalingInspectionError

        with self.assertRaises(ScalingInspectionError):
            Command()._inspect(LocalScalingPolicy.get_solo(), {"ingest": 1, "images": 1})
        autoscale.assert_not_called()

    @override_settings(LOCAL_SCALING_SLOT_BUDGET=4)
    @patch("datasets.management.commands.local_scaling_status.celery_app.control.inspect")
    def test_controller_refuses_a_persisted_policy_over_the_current_budget(self, inspect):
        policy = LocalScalingPolicy.objects.create(
            pk=1,
            ingest_max_processes=2,
            ingest_max_replicas=2,
            images_max_processes=2,
            images_max_replicas=2,
        )
        from datasets.management.commands.local_scaling_status import Command, ScalingInspectionError

        with self.assertRaises(ScalingInspectionError):
            Command()._inspect(policy, {"ingest": 1, "images": 1})
        inspect.assert_not_called()
