import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from redis import Redis

from config.celery import app as celery_app
from datasets.models import LocalScalingPolicy, LocalScalingStatus


class ScalingInspectionError(RuntimeError):
    pass


def _workers_for_queue(mapping, prefix, expected_replicas, label):
    if not isinstance(mapping, dict):
        raise ScalingInspectionError(f"Celery did not return {label} worker data")
    workers = {name: value for name, value in mapping.items() if name.startswith(f"{prefix}@")}
    if len(workers) != expected_replicas:
        raise ScalingInspectionError(
            f"Expected {expected_replicas} {prefix} workers, received {len(workers)} during {label} inspection"
        )
    return workers


def _acknowledged_workers(replies):
    workers = set()
    if isinstance(replies, list):
        for reply in replies:
            if isinstance(reply, dict):
                workers.update(
                    name for name, result in reply.items()
                    if isinstance(result, dict) and "ok" in result
                )
    return workers


class Command(BaseCommand):
    help = "Inspect local Celery workers and persist the latest scaling controller heartbeat."

    def add_arguments(self, parser):
        parser.add_argument("--ingest-replicas", type=int, default=1)
        parser.add_argument("--images-replicas", type=int, default=1)
        parser.add_argument("--report-error", default="")

    def handle(self, *args, **options):
        policy = LocalScalingPolicy.get_solo()
        now = timezone.now()
        status = LocalScalingStatus.get_solo()

        if options["report_error"]:
            status.state = LocalScalingStatus.State.DEGRADED
            status.last_seen_at = now
            status.error = options["report_error"][:500]
            status.save(update_fields=["state", "last_seen_at", "error", "updated_at"])
            self.stdout.write(json.dumps({"ok": False, "error": status.error}))
            return

        replica_counts = {"ingest": options["ingest_replicas"], "images": options["images_replicas"]}
        try:
            queues = self._inspect(policy, replica_counts)
        except Exception as exc:
            status.state = LocalScalingStatus.State.DEGRADED
            status.last_seen_at = now
            status.error = f"{type(exc).__name__}: {exc}"[:500]
            status.save(update_fields=["state", "last_seen_at", "error", "updated_at"])
            self.stdout.write(json.dumps({
                "ok": False,
                "policy": policy.as_dict(),
                "error": status.error,
            }))
            return

        status.state = LocalScalingStatus.State.HEALTHY
        status.last_seen_at = now
        status.error = ""
        status.queues = queues
        status.save(update_fields=["state", "last_seen_at", "error", "queues", "updated_at"])
        self.stdout.write(json.dumps({"ok": True, "policy": policy.as_dict(), "queues": queues}))

    def _inspect(self, policy, replica_counts):
        queues = policy.as_dict()["queues"]
        maximum_slots = sum(limits["max_processes"] * limits["max_replicas"] for limits in queues.values())
        if maximum_slots > settings.LOCAL_SCALING_SLOT_BUDGET:
            raise ScalingInspectionError(
                "Saved worker limits exceed the current slot budget; lower a queue maximum in Worker scaling."
            )

        inspector = celery_app.control.inspect(timeout=1.5)
        active = inspector.active()
        reserved = inspector.reserved()
        scheduled = inspector.scheduled()
        stats = inspector.stats()

        redis = Redis.from_url(
            celery_app.conf.broker_url,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
            decode_responses=True,
        )
        try:
            queue_depths = {queue: int(redis.llen(queue)) for queue in ("ingest", "images")}
        finally:
            redis.close()

        queue_config = queues
        result = {}
        worker_names_by_queue = {}
        for queue, worker_prefix in (("ingest", "ingest"), ("images", "images")):
            expected = replica_counts[queue]
            active_workers = _workers_for_queue(active, worker_prefix, expected, "active task")
            reserved_workers = _workers_for_queue(reserved, worker_prefix, expected, "reserved task")
            scheduled_workers = _workers_for_queue(scheduled, worker_prefix, expected, "scheduled task")
            stats_workers = _workers_for_queue(stats, worker_prefix, expected, "worker stats")

            process_count = 0
            active_count = 0
            reserved_count = 0
            scheduled_count = 0
            names = set(stats_workers)
            for worker_name in names:
                worker_stats = stats_workers[worker_name]
                pool = worker_stats.get("pool", {}) if isinstance(worker_stats, dict) else {}
                processes = pool.get("processes") if isinstance(pool, dict) else None
                if not isinstance(processes, (list, tuple, dict)):
                    raise ScalingInspectionError(f"Celery did not report process counts for {worker_name}")
                process_count += len(processes)
                active_count += len(active_workers.get(worker_name) or [])
                reserved_count += len(reserved_workers.get(worker_name) or [])
                scheduled_count += len(scheduled_workers.get(worker_name) or [])

            worker_names_by_queue[queue] = sorted(names)
            result[queue] = {
                "queued": queue_depths[queue],
                "active": active_count,
                "reserved": reserved_count,
                "scheduled": scheduled_count,
                "replicas": expected,
                "processes": process_count,
            }

        # Complete broker and worker inspection for both queues before making
        # any remote-control changes. A partial inspection must be read-only.
        for queue, worker_prefix in (("ingest", "ingest"), ("images", "images")):
            process_limits = queue_config[queue]
            names = worker_names_by_queue[queue]
            replies = celery_app.control.autoscale(
                process_limits["max_processes"],
                process_limits["min_processes"],
                destination=names,
                reply=True,
                timeout=1.5,
            )
            if _acknowledged_workers(replies) != set(names):
                raise ScalingInspectionError(f"Celery did not acknowledge process limits for all {worker_prefix} workers")
        return result
