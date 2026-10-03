import logging

from celery import current_app

logger = logging.getLogger(__name__)

TASKS = {
    "ingest_dataset": ("processing.tasks.ingest_dataset", "ingest"),
    "extract_metadata": ("processing.tasks.extract_metadata", "images"),
    "generate_thumbnail": ("processing.tasks.generate_thumbnail", "images"),
}


def enqueue_job(job_id, kind):
    task_name, queue = TASKS[kind]
    try:
        current_app.send_task(task_name, args=[str(job_id)], queue=queue, task_id=str(job_id))
    except Exception:
        # PostgreSQL remains authoritative. A worker's reconciliation task
        # will re-enqueue queued rows after a broker interruption.
        logger.exception("Could not enqueue job %s; it remains queued for recovery", job_id)


def enqueue_storage_cleanup():
    try:
        current_app.send_task("processing.tasks.cleanup_stored_objects", queue="ingest")
    except Exception:
        # The deletion rows remain durable and Celery Beat will retry them.
        logger.exception("Could not enqueue stored object cleanup; it remains queued for recovery")


def revoke_jobs(job_ids):
    """Revoke Celery tasks for database jobs, including pre-migration task IDs."""
    job_ids = {str(job_id) for job_id in job_ids}
    if not job_ids:
        return

    active_task_ids = set()
    celery_task_ids = set(job_ids)
    try:
        inspector = current_app.control.inspect(timeout=1)
        for task_group, is_active in ((inspector.active() or {}, True), (inspector.reserved() or {}, False), (inspector.scheduled() or {}, False)):
            for worker_tasks in task_group.values():
                for task in worker_tasks:
                    request = task.get("request", task)
                    args = request.get("args", [])
                    if isinstance(args, str):
                        # Celery normally returns a decoded list, but older
                        # tasks may expose their arguments as a string.
                        matches = any(job_id in args for job_id in job_ids)
                    else:
                        matches = bool(args) and str(args[0]) in job_ids
                    task_id = request.get("id") or task.get("id")
                    if matches and task_id:
                        celery_task_ids.add(str(task_id))
                        if is_active:
                            active_task_ids.add(str(task_id))
    except Exception:
        logger.exception("Could not inspect workers while cancelling jobs %s", sorted(job_ids))

    for task_id in celery_task_ids:
        try:
            current_app.control.revoke(
                task_id,
                terminate=task_id in active_task_ids,
                signal="SIGTERM",
                reply=False,
            )
        except Exception:
            # The database cancellation is authoritative. Even if remote
            # control is unavailable, cancelled jobs cannot be claimed again.
            logger.exception("Could not revoke Celery task %s", task_id)
