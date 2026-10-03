"""Idempotent Celery jobs for dataset ingestion and image processing."""

from __future__ import annotations

import random
from contextlib import ExitStack
from datetime import timedelta

from celery import shared_task
from django.contrib.gis.geos import Point
from django.db import IntegrityError, InterfaceError, OperationalError, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from datasets.models import Dataset, Image, Job, StoredObjectDeletion, UploadAsset
from datasets.storage import delete_objects, schedule_object_deletions

from .archive import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_SIZE_LABEL,
    MAX_TOTAL_BYTES,
    is_supported_image,
    iter_zip_images,
    safe_relative_path,
    validate_zip_archive,
)
from .exceptions import InvalidDataset, MissingObject, PermanentProcessingError, RetryableProcessingError
from .image_processing import make_derivatives, inspect_image
from .storage import download_to_file, stat_object, upload_file, upload_bytes

RETRY_LIMIT = 3
STALE_AFTER = timedelta(minutes=30)
REQUEUE_AFTER = timedelta(minutes=2)
SUPPORTED_JOB_TASKS = {
    Job.Kind.INGEST_DATASET: "processing.tasks.ingest_dataset",
    Job.Kind.EXTRACT_METADATA: "processing.tasks.extract_metadata",
    Job.Kind.GENERATE_THUMBNAIL: "processing.tasks.generate_thumbnail",
}


def _append_error(errors, stage, message, *, retryable=False):
    errors = list(errors or [])
    entry = {"stage": stage, "message": str(message)[:1000]}
    if retryable:
        entry["retryable"] = True
    if entry not in errors:
        errors.append(entry)
    return errors[-100:]


def _is_final_error(error):
    return not isinstance(error, dict) or error.get("stage") not in {"retry", "recovery"}


def _task_for_kind(kind):
    from celery import current_app

    task_name = SUPPORTED_JOB_TASKS.get(kind)
    return current_app.tasks.get(task_name) if task_name else None


def _dispatch(job_id, kind):
    task = _task_for_kind(kind)
    if task is None:
        raise RuntimeError(f"Celery task for job kind '{kind}' is not registered")
    queue = "ingest" if kind == Job.Kind.INGEST_DATASET else "images"
    try:
        task.apply_async(args=[str(job_id)], queue=queue, task_id=str(job_id))
    except Exception as exc:
        raise RetryableProcessingError(f"Could not publish processing task ({type(exc).__name__})") from exc


def _claim_job(job_id):
    """Claim one queued job; stale running work is reclaimed by reconciler."""
    with transaction.atomic():
        parent_id = Job.objects.filter(pk=job_id).values_list("parent_id", flat=True).first()
        parent = Job.objects.select_for_update(of=("self",)).filter(pk=parent_id).first() if parent_id else None
        job = Job.objects.select_for_update(of=("self",)).select_related("dataset", "image").filter(pk=job_id).first()
        if job is None or job.status in {
            Job.Status.SUCCEEDED,
            Job.Status.SUCCEEDED_WITH_ERRORS,
            Job.Status.FAILED,
            Job.Status.CANCELLED,
        }:
            return None
        if job.parent_id and (parent is None or parent.status == Job.Status.CANCELLED):
            job.status = Job.Status.CANCELLED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "updated_at"])
            return None
        if job.status == Job.Status.RUNNING:
            return None
        job.status = Job.Status.RUNNING
        job.attempt_count += 1
        job.started_at = job.started_at or timezone.now()
        job.save(update_fields=["status", "attempt_count", "started_at", "updated_at"])
        return job


def _mark_retrying(job_id, error):
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id).first()
        if job and job.status not in {
            Job.Status.SUCCEEDED,
            Job.Status.SUCCEEDED_WITH_ERRORS,
            Job.Status.FAILED,
            Job.Status.CANCELLED,
        }:
            job.status = Job.Status.QUEUED
            job.errors = _append_error(job.errors, "retry", error, retryable=True)
            job.save(update_fields=["status", "errors", "updated_at"])


def _set_failed(job_id, error):
    with transaction.atomic():
        parent_id = Job.objects.filter(pk=job_id).values_list("parent_id", flat=True).first()
        parent = Job.objects.select_for_update(of=("self",)).filter(pk=parent_id).first() if parent_id else None
        job = Job.objects.select_for_update(of=("self",)).select_related("image").filter(pk=job_id).first()
        if not job or job.status in {
            Job.Status.SUCCEEDED,
            Job.Status.SUCCEEDED_WITH_ERRORS,
            Job.Status.FAILED,
            Job.Status.CANCELLED,
        }:
            return
        if job.parent_id and (parent is None or parent.status == Job.Status.CANCELLED):
            job.status = Job.Status.CANCELLED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "updated_at"])
            return
        job.status = Job.Status.FAILED
        job.finished_at = timezone.now()
        if job.image_id:
            job.total_count = 1
            job.failed_count = 1
        elif job.kind == Job.Kind.INGEST_DATASET and job.total_count:
            job.failed_count = job.total_count
        job.errors = _append_error(job.errors, "processing", error)
        job.save(update_fields=["status", "finished_at", "total_count", "failed_count", "errors", "updated_at"])
        if job.image_id:
            Image.objects.filter(pk=job.image_id).update(
                status=Image.Status.FAILED,
                updated_at=timezone.now(),
            )
        if job.parent_id:
            _refresh_parent(job.parent_id)
        elif job.kind == Job.Kind.INGEST_DATASET:
            Dataset.objects.filter(pk=job.dataset_id).update(
                status=Dataset.Status.FAILED,
                updated_at=timezone.now(),
            )


def _mark_succeeded(job_id, result):
    with transaction.atomic():
        parent_id = Job.objects.filter(pk=job_id).values_list("parent_id", flat=True).first()
        parent = Job.objects.select_for_update(of=("self",)).filter(pk=parent_id).first() if parent_id else None
        job = Job.objects.select_for_update(of=("self",)).select_related("image").filter(pk=job_id).first()
        if not job or job.status in {
            Job.Status.SUCCEEDED,
            Job.Status.SUCCEEDED_WITH_ERRORS,
            Job.Status.FAILED,
            Job.Status.CANCELLED,
        }:
            return False
        if job.parent_id and (parent is None or parent.status == Job.Status.CANCELLED):
            job.status = Job.Status.CANCELLED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "updated_at"])
            return False
        if job.image_id and job.kind == Job.Kind.EXTRACT_METADATA:
            gps = result.get("gps")
            location = Point(*gps, srid=4326) if gps else None
            Image.objects.filter(pk=job.image_id).update(
                width=result["width"],
                height=result["height"],
                captured_at=parse_datetime(result["captured_at"]) if result.get("captured_at") else None,
                camera_make=result.get("camera_make") or "",
                camera_model=result.get("camera_model") or "",
                location=location,
                updated_at=timezone.now(),
            )
        elif job.image_id and job.kind == Job.Kind.GENERATE_THUMBNAIL:
            Image.objects.filter(pk=job.image_id).update(
                thumbnail_key=result["thumbnail_key"],
                preview_key=result["preview_key"],
                updated_at=timezone.now(),
            )
        job.status = Job.Status.SUCCEEDED
        job.result_data = result or {}
        job.finished_at = timezone.now()
        if job.image_id:
            job.total_count = 1
            job.completed_count = 1
            job.failed_count = 0
        job.save(update_fields=[
            "status", "result_data", "finished_at", "total_count", "completed_count", "failed_count", "updated_at"
        ])
        if job.image_id:
            _refresh_image(job.image_id)
        if job.parent_id:
            _refresh_parent(job.parent_id)
        return True


def _refresh_image(image_id):
    image = Image.objects.select_for_update().filter(pk=image_id).first()
    if not image:
        return
    needed = {Job.Kind.EXTRACT_METADATA, Job.Kind.GENERATE_THUMBNAIL}
    latest_status = {}
    for kind in needed:
        latest = Job.objects.filter(image_id=image_id, kind=kind).order_by("-created_at").first()
        latest_status[kind] = latest.status if latest else None
    if Job.Status.FAILED in latest_status.values():
        image.status = Image.Status.FAILED
    elif all(latest_status[kind] in {Job.Status.SUCCEEDED, Job.Status.SUCCEEDED_WITH_ERRORS} for kind in needed):
        image.status = Image.Status.PROCESSED
    elif Job.Status.CANCELLED in latest_status.values():
        image.status = Image.Status.PENDING
    else:
        image.status = Image.Status.PROCESSING
    image.save(update_fields=["status", "updated_at"])


def _refresh_parent(parent_id):
    """Aggregate child image jobs into the parent dataset's image progress."""
    parent = Job.objects.select_for_update().filter(pk=parent_id).first()
    if not parent or parent.kind != Job.Kind.INGEST_DATASET or parent.status == Job.Status.CANCELLED:
        return
    if "images_created" not in (parent.result_data or {}):
        # The ingest task has not committed its image set yet.
        return
    child_jobs = Job.objects.filter(parent_id=parent_id, image_id__isnull=False)
    terminal = {Job.Status.SUCCEEDED, Job.Status.SUCCEEDED_WITH_ERRORS, Job.Status.FAILED, Job.Status.CANCELLED}
    completed_count = 0
    failed_count = 0
    latest_metadata = child_jobs.filter(
        image_id=OuterRef("image_id"), kind=Job.Kind.EXTRACT_METADATA
    ).order_by("-created_at", "-pk").values("status")[:1]
    latest_preview = child_jobs.filter(
        image_id=OuterRef("image_id"), kind=Job.Kind.GENERATE_THUMBNAIL
    ).order_by("-created_at", "-pk").values("status")[:1]
    image_states = list(
        child_jobs.order_by()
        .values("image_id")
        .distinct()
        .annotate(metadata_status=Subquery(latest_metadata), preview_status=Subquery(latest_preview))
        .values_list("image_id", "metadata_status", "preview_status")
    )
    for _image_id, metadata_status, preview_status in image_states:
        states = {
            Job.Kind.EXTRACT_METADATA: metadata_status,
            Job.Kind.GENERATE_THUMBNAIL: preview_status,
        }
        kinds = {Job.Kind.EXTRACT_METADATA, Job.Kind.GENERATE_THUMBNAIL}
        if not kinds.issubset(states) or any(states[kind] not in terminal for kind in kinds):
            continue
        if any(states[kind] == Job.Status.FAILED for kind in kinds):
            failed_count += 1
        else:
            completed_count += 1

    ingest_failures = int((parent.result_data or {}).get("ingest_failed_count", 0))
    imported_images = int((parent.result_data or {}).get("images_created", 0))
    image_total = max(imported_images, len(image_states))
    total_count = image_total + ingest_failures
    parent.total_count = total_count
    parent.completed_count = completed_count
    parent.failed_count = failed_count + ingest_failures
    all_terminal = completed_count + failed_count == image_total
    if all_terminal and parent.status not in {Job.Status.FAILED, Job.Status.CANCELLED}:
        if image_states or ingest_failures:
            parent.status = Job.Status.SUCCEEDED_WITH_ERRORS if failed_count or ingest_failures else Job.Status.SUCCEEDED
        else:
            parent.status = Job.Status.FAILED
        parent.finished_at = timezone.now()
        parent.result_data = {
            **(parent.result_data or {}),
            "images_total": total_count,
            "images_succeeded": completed_count,
            "images_failed": failed_count,
        }
        dataset_status = Dataset.Status.COMPLETED if completed_count else Dataset.Status.FAILED
        Dataset.objects.filter(pk=parent.dataset_id).update(status=dataset_status, updated_at=timezone.now())
    parent.save(update_fields=[
        "status", "total_count", "completed_count", "failed_count", "result_data", "finished_at", "updated_at"
    ])


def _finish_ingest(job_id, images_created, errors):
    with transaction.atomic():
        parent = Job.objects.select_for_update().filter(pk=job_id).first()
        if not parent or parent.status == Job.Status.CANCELLED:
            return False
        combined_errors = list(parent.errors or [])
        for error in errors:
            if error not in combined_errors:
                combined_errors.append(error)
        final_errors = [error for error in combined_errors if _is_final_error(error)]
        ingest_failures = len(final_errors)
        parent.total_count = images_created + ingest_failures
        parent.completed_count = 0
        parent.failed_count = ingest_failures
        parent.errors = combined_errors[-100:]
        parent.result_data = {
            **(parent.result_data or {}),
            "images_created": images_created,
            "ingest_failed_count": ingest_failures,
        }
        if images_created == 0:
            parent.status = Job.Status.FAILED
            parent.finished_at = timezone.now()
            Dataset.objects.filter(pk=parent.dataset_id).update(
                status=Dataset.Status.FAILED,
                updated_at=timezone.now(),
            )
        else:
            # The parent remains running until all child jobs finish.
            parent.status = Job.Status.RUNNING
            Dataset.objects.filter(pk=parent.dataset_id).update(
                status=Dataset.Status.PROCESSING,
                updated_at=timezone.now(),
            )
        parent.save(update_fields=[
            "status", "total_count", "completed_count", "failed_count", "errors", "result_data", "finished_at", "updated_at"
        ])
        _refresh_parent(parent.pk)
        return True


def _create_image_jobs(image, parent):
    created = []
    for kind in (Job.Kind.EXTRACT_METADATA, Job.Kind.GENERATE_THUMBNAIL):
        job = Job.objects.filter(image=image, kind=kind, parent=parent).order_by("-created_at").first()
        if job is None:
            active_other = Job.objects.filter(
                image=image,
                kind=kind,
                status__in=Job.ACTIVE_STATUSES,
            ).order_by("-created_at").first()
            if active_other:
                if active_other.parent_id != parent.pk:
                    active_other.parent = parent
                    active_other.save(update_fields=["parent", "updated_at"])
                if active_other.status == Job.Status.QUEUED:
                    created.append((active_other.pk, kind))
                continue
            try:
                job = Job.objects.create(
                    dataset=image.dataset,
                    image=image,
                    parent=parent,
                    kind=kind,
                    status=Job.Status.QUEUED,
                    input_data={"image_id": str(image.pk)},
                )
                created.append((job.pk, kind))
            except IntegrityError:
                job = Job.objects.filter(
                    image=image,
                    kind=kind,
                    status__in=Job.ACTIVE_STATUSES,
                ).order_by("-created_at").first()
                if job and job.parent_id != parent.pk:
                    job.parent = parent
                    job.save(update_fields=["parent", "updated_at"])
        elif job.status == Job.Status.QUEUED:
            created.append((job.pk, kind))
    return created


def _validate_direct_asset(asset):
    relative_path = safe_relative_path(asset.relative_path)
    if not is_supported_image(relative_path):
        raise InvalidDataset(f"Unsupported image type: {relative_path}")
    info = stat_object(asset.object_key)
    actual_size = int(info.get("ContentLength", 0))
    if actual_size <= 0 or actual_size > MAX_IMAGE_BYTES:
        raise InvalidDataset(f"Uploaded image size is invalid: {relative_path}")
    if asset.expected_size and actual_size != asset.expected_size:
        raise InvalidDataset(f"Uploaded image size did not match: {relative_path}")
    return {
        "relative_path": relative_path,
        "object_key": asset.object_key,
        "content_type": asset.content_type or "application/octet-stream",
        "size": actual_size,
    }


def _validate_archive_upload(asset, archive_file):
    info = stat_object(asset.object_key)
    archive_size = int(info.get("ContentLength", 0))
    if archive_size <= 0 or archive_size > MAX_TOTAL_BYTES:
        raise InvalidDataset("Uploaded ZIP size is invalid")
    if asset.expected_size and archive_size != asset.expected_size:
        raise InvalidDataset("Uploaded ZIP size did not match")
    return validate_zip_archive(archive_file, image_validator=inspect_image)


def _schedule_unreferenced_archive_objects(object_keys):
    if not object_keys:
        return
    used_keys = set(Image.objects.filter(object_key__in=object_keys).values_list("object_key", flat=True))
    schedule_object_deletions(set(object_keys) - used_keys)


def _import_archive_asset(asset, dataset_id, archive_file, object_keys):
    imported = []
    archive_file.seek(0)
    # Every selected ZIP is fully validated before this import pass creates
    # rows or writes image objects.
    for entry in iter_zip_images(archive_file, str(dataset_id), asset.object_key):
        try:
            # Track before writing so a partial object-store write is also
            # eligible for cleanup if a later entry fails.
            object_keys.append(entry.object_key)
            existing = Image.objects.filter(dataset_id=dataset_id, relative_path=entry.relative_path).first()
            if existing and existing.object_key != entry.object_key:
                raise InvalidDataset(f"More than one uploaded file uses the path {entry.relative_path}")
            # Re-upload stable keys on replay if a prior attempt committed the
            # row before it finished writing the object.
            try:
                stat_object(entry.object_key)
            except MissingObject:
                upload_file(entry.object_key, entry.data, entry.size, entry.content_type)
            imported.append({
                "relative_path": entry.relative_path,
                "object_key": entry.object_key,
                "content_type": entry.content_type,
                "size": entry.size,
            })
        finally:
            entry.data.close()
    return imported


def _get_image_for_job(job):
    if job.image_id:
        return job.image
    image_id = (job.input_data or {}).get("image_id")
    if not image_id:
        raise InvalidDataset("Image job is missing image_id")
    try:
        image = Image.objects.get(pk=image_id, dataset_id=job.dataset_id)
    except Image.DoesNotExist as exc:
        raise InvalidDataset("The requested image does not exist") from exc
    if job.image_id is None:
        Job.objects.filter(pk=job.pk, image__isnull=True).update(image=image)
        job.image = image
    return image


def _execute(task, job_id, operation):
    try:
        job = _claim_job(job_id)
        if job is None:
            return {"job_id": str(job_id), "status": "already_claimed_or_terminal"}
        if job.kind != operation.expected_kind:
            raise PermanentProcessingError("Job kind does not match its Celery task")
        result = operation(job)
        if operation.expected_kind == Job.Kind.INGEST_DATASET:
            # Keep the parent active until its image children have completed.
            current = Job.objects.only("status").get(pk=job_id)
            return {"job_id": str(job_id), "status": current.status, "result": result}
        marked = _mark_succeeded(job_id, result)
        if marked is False:
            keys = [result.get("thumbnail_key"), result.get("preview_key")]
            schedule_object_deletions(keys)
            return {"job_id": str(job_id), "status": "cancelled"}
        return {"job_id": str(job_id), "status": "succeeded", "result": result}
    except PermanentProcessingError as exc:
        _set_failed(job_id, exc)
        return {"job_id": str(job_id), "status": "failed", "error": str(exc)}
    except (RetryableProcessingError, OperationalError, InterfaceError) as exc:
        retries = task.request.retries
        if retries < RETRY_LIMIT:
            _mark_retrying(job_id, exc)
            countdown = min(2 ** retries, 60) + random.uniform(0, 1)
            raise task.retry(exc=exc, countdown=countdown, max_retries=RETRY_LIMIT)
        _set_failed(job_id, f"Failed after {RETRY_LIMIT} retries: {exc}")
        return {"job_id": str(job_id), "status": "failed", "error": str(exc)}
    except Exception as exc:
        # Unexpected application errors should be visible and terminal. A
        # blanket retry can hide deterministic bugs and repeatedly mutate data.
        _set_failed(job_id, f"Unexpected processing error ({type(exc).__name__})")
        raise


def _ingest_operation(job):
    requested_ids = (job.input_data or {}).get("upload_asset_ids")
    asset_query = UploadAsset.objects.filter(dataset_id=job.dataset_id, confirmed=True)
    if "upload_asset_ids" in (job.input_data or {}):
        if not requested_ids:
            raise InvalidDataset("No confirmed uploads were selected")
        asset_query = asset_query.filter(pk__in=requested_ids)
    assets = list(asset_query.order_by("created_at", "id"))
    if not assets:
        raise InvalidDataset("No confirmed uploads are available to process")

    image_ids = []
    image_items = []
    archive_object_keys = []
    child_jobs = []
    failures = []
    total_bytes = 0
    count = 0
    if any(asset.is_archive for asset in assets):
        if not all(asset.is_archive for asset in assets):
            raise InvalidDataset("Upload either ZIP archives or individual images, not both")
        with ExitStack() as stack:
            prepared_archives = []
            seen_image_paths = set()
            expanded_bytes = 0
            file_count = 0
            # Keep each compressed archive staged while validating every
            # selected archive before any imported row or image object exists.
            for asset in assets:
                if asset.upload_error:
                    failures.append({"path": asset.relative_path, "message": asset.upload_error})
                    continue
                archive_file = stack.enter_context(download_to_file(asset.object_key))
                manifest = _validate_archive_upload(asset, archive_file)
                file_count += manifest.file_count
                expanded_bytes += manifest.expanded_bytes
                if file_count > MAX_IMAGES:
                    raise InvalidDataset("A dataset may contain at most 1,000 files")
                if expanded_bytes > MAX_TOTAL_BYTES:
                    raise InvalidDataset(f"Expanded dataset size exceeds {MAX_SIZE_LABEL}")
                for path in manifest.paths:
                    if is_supported_image(path):
                        if path in seen_image_paths:
                            raise InvalidDataset(f"More than one ZIP entry uses the path {path}")
                        seen_image_paths.add(path)
                prepared_archives.append((asset, archive_file, manifest))

            total_bytes = expanded_bytes
            imported_images = []
            try:
                for asset, archive_file, manifest in prepared_archives:
                    if not manifest.image_count:
                        failures.append({"path": asset.relative_path, "message": "ZIP contains no supported images"})
                        continue
                    imported = _import_archive_asset(asset, job.dataset_id, archive_file, archive_object_keys)
                    imported_images.extend(imported)
            except Exception:
                _schedule_unreferenced_archive_objects(archive_object_keys)
                raise
            image_items = imported_images
            count = len(image_items)
    else:
        validated_files = []
        for asset in assets:
            if asset.upload_error:
                failures.append({"path": asset.relative_path, "message": asset.upload_error})
                continue
            try:
                if count >= MAX_IMAGES:
                    raise InvalidDataset("A dataset may contain at most 1,000 images")
                item = _validate_direct_asset(asset)
                if item["size"] + total_bytes > MAX_TOTAL_BYTES:
                    raise InvalidDataset(f"Dataset size exceeds {MAX_SIZE_LABEL}")
                validated_files.append(item)
                total_bytes += item["size"]
                count += 1
            except PermanentProcessingError as exc:
                failures.append({"path": asset.relative_path, "message": str(exc)})
            except RetryableProcessingError:
                raise
        image_items = validated_files

    if count > MAX_IMAGES or total_bytes > MAX_TOTAL_BYTES:
        raise InvalidDataset(f"Dataset exceeds its file count or {MAX_SIZE_LABEL} size limit")

    cancelled = False
    try:
        with transaction.atomic():
            current_job = Job.objects.select_for_update(of=("self",)).filter(pk=job.pk).first()
            if current_job is None or current_job.status != Job.Status.RUNNING:
                cancelled = True
            else:
                images = []
                for item in image_items:
                    image, _created = Image.objects.get_or_create(
                        dataset_id=job.dataset_id,
                        relative_path=item["relative_path"],
                        defaults={
                            "object_key": item["object_key"],
                            "content_type": item["content_type"],
                            "size": item["size"],
                        },
                    )
                    if image.object_key != item["object_key"]:
                        raise InvalidDataset(f"More than one uploaded file uses the path {item['relative_path']}")
                    images.append(image)
                    image_ids.append(image.pk)
                for image in images:
                    child_jobs.extend(_create_image_jobs(image, job))
                Image.objects.filter(pk__in=image_ids).update(status=Image.Status.PROCESSING, updated_at=timezone.now())
    except Exception:
        _schedule_unreferenced_archive_objects(archive_object_keys)
        raise
    if cancelled:
        _schedule_unreferenced_archive_objects(archive_object_keys)
        return {"cancelled": True, "images_created": 0}
    if not _finish_ingest(str(job.pk), len(image_ids), failures):
        return {"cancelled": True, "images_created": len(image_ids)}

    # Publish after the parent and children are durable. If a broker is down,
    # retrying this idempotent ingest job reuses rows and republishes queued work.
    for child_id, kind in child_jobs:
        _dispatch(child_id, kind)
    return {"images_created": len(image_ids), "failed_uploads": len(failures)}


_ingest_operation.expected_kind = Job.Kind.INGEST_DATASET


def _metadata_operation(job):
    image = _get_image_for_job(job)
    with download_to_file(image.object_key) as image_file:
        metadata = inspect_image(image_file)
    return {
        "image_id": str(image.pk),
        "width": metadata["width"],
        "height": metadata["height"],
        "captured_at": metadata["captured_at"].isoformat() if metadata["captured_at"] else None,
        "camera_make": metadata["camera_make"] or "",
        "camera_model": metadata["camera_model"] or "",
        "gps": list(metadata["gps"]) if metadata["gps"] else None,
        "has_location": bool(metadata["gps"]),
    }


_metadata_operation.expected_kind = Job.Kind.EXTRACT_METADATA


def _thumbnail_operation(job):
    image = _get_image_for_job(job)
    with download_to_file(image.object_key) as image_file:
        thumbnail, preview = make_derivatives(image_file)
    thumbnail_key = f"datasets/{image.dataset_id}/derived/{image.pk}/{job.pk}/thumbnail.webp"
    preview_key = f"datasets/{image.dataset_id}/derived/{image.pk}/{job.pk}/preview.webp"
    upload_bytes(thumbnail_key, thumbnail, "image/webp")
    upload_bytes(preview_key, preview, "image/webp")
    return {
        "image_id": str(image.pk),
        "thumbnail_key": thumbnail_key,
        "preview_key": preview_key,
        "thumbnail_size": len(thumbnail),
        "preview_size": len(preview),
    }


_thumbnail_operation.expected_kind = Job.Kind.GENERATE_THUMBNAIL


@shared_task(bind=True, name="processing.tasks.cleanup_stored_objects", max_retries=3, acks_late=True)
def cleanup_stored_objects(self, batch_size=1000):
    """Delete committed object-store outbox entries; Beat retries failures."""
    rows = list(StoredObjectDeletion.objects.order_by("created_at", "pk").values_list("pk", "object_key")[:batch_size])
    if not rows:
        return {"deleted": 0}
    try:
        delete_objects([object_key for _pk, object_key in rows])
    except Exception as exc:
        if self.request.retries < RETRY_LIMIT:
            raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 60), max_retries=RETRY_LIMIT)
        raise
    StoredObjectDeletion.objects.filter(pk__in=[pk for pk, _object_key in rows]).delete()
    return {"deleted": len(rows)}


@shared_task(bind=True, name="processing.tasks.ingest_dataset", max_retries=RETRY_LIMIT, acks_late=True)
def ingest_dataset(self, job_id):
    return _execute(self, job_id, _ingest_operation)


@shared_task(bind=True, name="processing.tasks.extract_metadata", max_retries=RETRY_LIMIT, acks_late=True)
def extract_metadata(self, job_id):
    return _execute(self, job_id, _metadata_operation)


@shared_task(bind=True, name="processing.tasks.generate_thumbnail", max_retries=RETRY_LIMIT, acks_late=True)
def generate_thumbnail(self, job_id):
    return _execute(self, job_id, _thumbnail_operation)


@shared_task(bind=True, name="processing.tasks.reconcile_jobs", acks_late=True)
def reconcile_jobs(self, batch_size=200):
    """Republish old queued jobs and recover work left running after a crash."""
    now = timezone.now()
    queued_before = now - REQUEUE_AFTER
    stale_before = now - STALE_AFTER
    dispatch = []
    with transaction.atomic():
        cancelled_children = list(
            Job.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status__in=Job.ACTIVE_STATUSES, parent__status=Job.Status.CANCELLED)
            .order_by("updated_at")[:batch_size]
        )
        for job in cancelled_children:
            job.status = Job.Status.CANCELLED
            job.finished_at = now
            job.save(update_fields=["status", "finished_at", "updated_at"])

        remaining = max(batch_size - len(cancelled_children), 0)
        queued = list(
            Job.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status=Job.Status.QUEUED, updated_at__lt=queued_before)
            .exclude(parent__status=Job.Status.CANCELLED)
            .order_by("updated_at")[:remaining]
        )
        stale = list(
            Job.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status=Job.Status.RUNNING, updated_at__lt=stale_before)
            .exclude(parent__status=Job.Status.CANCELLED)
            .order_by("updated_at")[: max(remaining - len(queued), 0)]
        )
        for job in queued + stale:
            if job.kind not in SUPPORTED_JOB_TASKS:
                continue
            if job.status == Job.Status.RUNNING:
                job.status = Job.Status.QUEUED
                job.errors = _append_error(job.errors, "recovery", "Requeued after worker interruption", retryable=True)
            job.save(update_fields=["status", "errors", "updated_at"])
            dispatch.append((job.pk, job.kind))

        # Older versions could leave a parent session running after all of
        # its image tasks reached terminal states. Recompute those counters
        # periodically so existing sessions recover without new task events.
        parent_ids = list(
            Job.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(
                kind=Job.Kind.INGEST_DATASET,
                status=Job.Status.RUNNING,
                result_data__has_key="images_created",
                updated_at__lt=queued_before,
            )
            .order_by("updated_at")
            .values_list("pk", flat=True)[:batch_size]
        )
        for parent_id in parent_ids:
            _refresh_parent(parent_id)
    sent = 0
    for job_id, kind in dispatch:
        try:
            _dispatch(job_id, kind)
            sent += 1
        except Exception:
            # Keep the job queued; a later reconciliation pass retries it.
            continue
    return {"requeued": sent, "considered": len(dispatch)}
