import logging
import posixpath
import uuid
from pathlib import PurePosixPath

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Dataset, Image, Job, UploadAsset
from .pagination import JobPagination, StandardPagination
from .serializers import DatasetSerializer, ImageSerializer, JobSerializer, UploadAssetSerializer
from .storage import (
    SINGLE_POST_MAX_BYTES,
    complete_multipart_upload,
    ensure_bucket_and_cors,
    object_size,
    presigned_multipart_upload,
    presigned_upload,
    schedule_object_deletions,
)
from .tasks_dispatch import enqueue_job, revoke_jobs

logger = logging.getLogger(__name__)
User = get_user_model()

MAX_IMAGES = settings.UPLOAD_MAX_FILES
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".geotiff": "image/tiff",
}
ARCHIVE_TYPES = {".zip": "application/zip"}


def error_response(message, code="invalid_request", http_status=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"error": {"code": code, "message": message, **extra}}, status=http_status)


def parse_uuid_list(values, field):
    if not isinstance(values, list):
        raise ValidationError({field: "Must be a list of UUIDs."})
    try:
        parsed = [uuid.UUID(str(value)) for value in values]
    except (ValueError, AttributeError, TypeError):
        raise ValidationError({field: "Every value must be a valid UUID."})
    if len(parsed) != len(set(parsed)):
        raise ValidationError({field: "Duplicate IDs are not allowed."})
    return parsed


def get_owned_dataset(user, dataset_id):
    try:
        return Dataset.objects.get(pk=dataset_id, owner=user)
    except (Dataset.DoesNotExist, ValueError):
        raise Http404


def _image_object_keys(images):
    keys = []
    for image in images:
        keys.extend(key for key in (image.object_key, image.thumbnail_key, image.preview_key) if key)
    return keys


def _delete_objects_after_commit(object_keys):
    schedule_object_deletions(object_keys)


def _refresh_dataset_status(dataset_id):
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None:
        return
    images = Image.objects.filter(dataset_id=dataset_id)
    if Job.objects.filter(dataset_id=dataset_id, status__in=Job.ACTIVE_STATUSES).exists() or images.filter(
        status=Image.Status.PROCESSING
    ).exists():
        state = Dataset.Status.PROCESSING
    elif images.filter(status=Image.Status.PROCESSED).exists():
        state = Dataset.Status.COMPLETED
    elif images.exists():
        state = Dataset.Status.FAILED if images.filter(status=Image.Status.FAILED).exists() else Dataset.Status.CREATED
    elif dataset.uploads.filter(confirmed=False).exists():
        state = Dataset.Status.UPLOADING
    else:
        state = Dataset.Status.CREATED
    Dataset.objects.filter(pk=dataset_id).update(status=state, updated_at=timezone.now())


def safe_relative_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError({"relative_path": "A non-empty relative path is required."})
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or normalized.startswith("/")
        or "\x00" in normalized
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ValidationError({"relative_path": "Path must be relative and cannot contain traversal segments."})
    if len(normalized) > 1024:
        raise ValidationError({"relative_path": "Path is too long."})
    return posixpath.normpath(normalized)


def dataset_counts(user):
    return Dataset.objects.filter(owner=user).annotate(
        image_count=Count("images", distinct=True),
        geotagged_count=Count("images", filter=Q(images__location__isnull=False), distinct=True),
    )


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CSRFView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username", "")
        password = request.data.get("password", "")
        email = request.data.get("email", "")
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 150 or not isinstance(password, str) or not password:
            return error_response("Username and password are required.")
        username = username.strip()
        if not isinstance(email, str):
            return error_response("Email must be a string.")
        email = email.strip()
        if User.objects.filter(username__iexact=username).exists():
            return error_response("That username is already in use.", code="username_taken", http_status=409)
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            return error_response("Password does not meet the requirements.", code="invalid_password", details=exc.messages)
        user = User.objects.create_user(username=username, email=email, password=password)
        login(request, user)
        return Response({"user": user_payload(user)}, status=status.HTTP_201_CREATED)


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username", "")
        password = request.data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            return error_response("Username and password are required.", code="invalid_credentials", http_status=401)
        username = username.strip()
        user = authenticate(request, username=username, password=password)
        if user is None:
            return error_response("Invalid username or password.", code="invalid_credentials", http_status=401)
        login(request, user)
        return Response({"user": user_payload(user)})


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response({"user": user_payload(request.user)})


def user_payload(user):
    return {
        "id": user.pk,
        "username": user.get_username(),
        "email": user.email,
        "local_scaling_available": settings.LOCAL_SCALING_ENABLED,
    }


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("Database readiness check failed")
            return Response({"status": "unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"status": "ok"})


class DatasetListCreateView(APIView):
    def get(self, request):
        queryset = dataset_counts(request.user).order_by("-created_at")
        page = StandardPagination()
        return page.get_paginated_response(DatasetSerializer(page.paginate_queryset(queryset, request), many=True).data)

    def post(self, request):
        name = request.data.get("name", "")
        if not isinstance(name, str) or not name.strip():
            return error_response("Dataset name is required.")
        if len(name.strip()) > 160:
            return error_response("Dataset name must be at most 160 characters.")
        dataset = Dataset.objects.create(owner=request.user, name=name.strip())
        data = DatasetSerializer(dataset_counts(request.user).get(pk=dataset.pk)).data
        return Response(data, status=status.HTTP_201_CREATED)


class DatasetDetailView(APIView):
    def get(self, request, dataset_id):
        dataset = get_owned_dataset(request.user, dataset_id)
        return Response(DatasetSerializer(dataset_counts(request.user).get(pk=dataset.pk)).data)

    def delete(self, request, dataset_id):
        object_keys = []
        with transaction.atomic():
            dataset = Dataset.objects.select_for_update().filter(pk=dataset_id, owner=request.user).first()
            if dataset is None:
                raise Http404
            jobs = list(Job.objects.select_for_update().filter(dataset=dataset).only("status"))
            if any(job.status in Job.ACTIVE_STATUSES for job in jobs):
                return error_response(
                    "Wait for processing to finish before deleting this dataset.",
                    code="processing_active",
                    http_status=status.HTTP_409_CONFLICT,
                )
            object_keys.extend(_image_object_keys(Image.objects.filter(dataset=dataset)))
            object_keys.extend(dataset.uploads.values_list("object_key", flat=True))
            dataset.delete()
            _delete_objects_after_commit(object_keys)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UploadPrepareView(APIView):
    def post(self, request, dataset_id):
        with transaction.atomic():
            dataset = Dataset.objects.select_for_update().filter(pk=dataset_id, owner=request.user).first()
            if dataset is None:
                raise Http404
            return self._prepare_locked(request, dataset)

    def _prepare_locked(self, request, dataset):
        files = request.data.get("files", [])
        if not isinstance(files, list) or not files:
            return error_response("Provide at least one file in `files`.")
        if len(files) > settings.UPLOAD_MAX_FILES:
            return error_response(f"At most {settings.UPLOAD_MAX_FILES} files can be uploaded in one dataset.")

        prepared = []
        existing_assets = list(dataset.uploads.all())
        existing_by_path = {asset.relative_path: asset for asset in existing_assets}
        incoming_paths = set()
        upload_modes = set()
        plans = []
        for item in files:
            if not isinstance(item, dict):
                raise ValidationError({"files": "Each file entry must be an object."})
            path = safe_relative_path(item.get("relative_path") or item.get("name"))
            if path in incoming_paths:
                raise ValidationError({"files": f"The path {path} appears more than once."})
            incoming_paths.add(path)
            prior_asset = existing_by_path.get(path)
            if prior_asset and prior_asset.confirmed:
                raise ValidationError({"files": f"{path} is already uploaded and confirmed."})
            suffix = PurePosixPath(path).suffix.lower()
            if suffix in ARCHIVE_TYPES:
                is_archive = True
                content_type = ARCHIVE_TYPES[suffix]
                mode = Dataset.UploadMode.ARCHIVE
            elif suffix in IMAGE_TYPES:
                is_archive = False
                content_type = IMAGE_TYPES[suffix]
                mode = Dataset.UploadMode.FOLDER
            else:
                raise ValidationError({"files": f"Unsupported file type for {path}. Use JPEG, PNG, WebP, TIFF/GeoTIFF, or ZIP."})
            try:
                raw_size = item.get("size")
                if isinstance(raw_size, bool) or not isinstance(raw_size, int):
                    raise ValueError
                size = raw_size
            except (TypeError, ValueError):
                raise ValidationError({"files": f"A valid size is required for {path}."})
            if size < 1 or size > settings.UPLOAD_MAX_BYTES:
                raise ValidationError({"files": f"File size for {path} is outside the allowed range."})
            plans.append((path, size, content_type, is_archive, mode, prior_asset))
            upload_modes.add(mode)

        new_paths = sum(1 for path in incoming_paths if path not in existing_by_path)
        if len(existing_assets) + new_paths > settings.UPLOAD_MAX_FILES:
            return error_response(f"A dataset can contain at most {settings.UPLOAD_MAX_FILES} upload items.")
        aggregate_size = sum(
            asset.expected_size for asset in existing_assets if asset.relative_path not in incoming_paths
        ) + sum(plan[1] for plan in plans)
        if aggregate_size > settings.UPLOAD_MAX_BYTES:
            size_limit_label = f"{settings.UPLOAD_MAX_BYTES / (1024 ** 3):g} GB"
            return error_response(f"Combined upload size exceeds the {size_limit_label} dataset limit.")
        if len(upload_modes) > 1:
            return error_response("Upload either a ZIP archive or image files, not both.")
        selected_mode = next(iter(upload_modes))
        if existing_assets and dataset.upload_mode != selected_mode:
            return error_response("Upload mode cannot change after files have been prepared.")

        ensure_bucket_and_cors()
        with transaction.atomic():
            if not existing_assets:
                dataset.upload_mode = selected_mode
                dataset.save(update_fields=["upload_mode", "updated_at"])
            for path, size, content_type, is_archive, _mode, prior_asset in plans:
                asset_id = prior_asset.pk if prior_asset else uuid.uuid4()
                basename = PurePosixPath(path).name
                revision = uuid.uuid4()
                object_key = f"users/{request.user.pk}/datasets/{dataset.pk}/uploads/{asset_id}/{revision}/{basename}"
                if prior_asset:
                    old_key = prior_asset.object_key
                    prior_asset.object_key = object_key
                    prior_asset.expected_size = size
                    prior_asset.content_type = content_type
                    prior_asset.is_archive = is_archive
                    prior_asset.confirmed = False
                    prior_asset.upload_error = ""
                    prior_asset.save()
                    if old_key != object_key:
                        _delete_objects_after_commit([old_key])
                    asset = prior_asset
                else:
                    asset = UploadAsset.objects.create(
                        id=asset_id,
                        dataset=dataset,
                        relative_path=path,
                        object_key=object_key,
                        expected_size=size,
                        content_type=content_type,
                        is_archive=is_archive,
                    )
                if size > SINGLE_POST_MAX_BYTES:
                    multipart = presigned_multipart_upload(object_key, content_type, size)
                    prepared.append({
                        **UploadAssetSerializer(asset).data,
                        "object_key": object_key,
                        "upload_url": "",
                        "fields": {},
                        "method": "MULTIPART",
                        "multipart": multipart,
                    })
                else:
                    target = presigned_upload(object_key, content_type, size)
                    prepared.append(
                        {
                            **UploadAssetSerializer(asset).data,
                            "object_key": object_key,
                            "upload_url": target["url"],
                            "fields": target["fields"],
                            "method": "POST",
                        }
                    )
        return Response({"uploads": prepared}, status=status.HTTP_201_CREATED)


class UploadConfirmView(APIView):
    def post(self, request, dataset_id):
        with transaction.atomic():
            dataset = Dataset.objects.select_for_update().filter(pk=dataset_id, owner=request.user).first()
            if dataset is None:
                raise Http404
            return self._confirm_locked(request, dataset)

    def _confirm_locked(self, request, dataset):
        upload_ids = request.data.get("upload_ids", [])
        if not isinstance(upload_ids, list) or not upload_ids:
            return error_response("Provide one or more upload_ids.")
        parsed_ids = parse_uuid_list(upload_ids, "upload_ids")
        assets = list(dataset.uploads.filter(pk__in=parsed_ids))
        if len(assets) != len(parsed_ids):
            return error_response("One or more upload IDs do not belong to this dataset.")
        multipart_uploads = request.data.get("multipart_uploads", [])
        if not isinstance(multipart_uploads, list):
            raise ValidationError({"multipart_uploads": "Must be a list."})
        multipart_by_asset = {}
        for item in multipart_uploads:
            if not isinstance(item, dict):
                raise ValidationError({"multipart_uploads": "Each entry must be an object."})
            try:
                asset_id = uuid.UUID(str(item.get("id")))
            except (ValueError, TypeError, AttributeError):
                raise ValidationError({"multipart_uploads": "Each entry needs a valid upload ID."})
            upload_id = item.get("upload_id")
            if not isinstance(upload_id, str) or not upload_id or len(upload_id) > 1024:
                raise ValidationError({"multipart_uploads": "Each entry needs a valid multipart upload ID."})
            if asset_id in multipart_by_asset:
                raise ValidationError({"multipart_uploads": "Duplicate upload IDs are not allowed."})
            multipart_by_asset[asset_id] = upload_id
        if set(multipart_by_asset) - set(parsed_ids):
            raise ValidationError({"multipart_uploads": "Multipart entries must match upload_ids."})
        for asset in assets:
            if asset.expected_size > SINGLE_POST_MAX_BYTES and asset.pk not in multipart_by_asset:
                raise ValidationError({"multipart_uploads": "Files larger than 5 GB must be completed as multipart uploads."})
            if asset.expected_size <= SINGLE_POST_MAX_BYTES and asset.pk in multipart_by_asset:
                raise ValidationError({"multipart_uploads": "Multipart details were provided for a standard upload."})
        results = []
        for asset in assets:
            try:
                if asset.pk in multipart_by_asset:
                    complete_multipart_upload(asset.object_key, multipart_by_asset[asset.pk], asset.expected_size)
                actual_size = object_size(asset.object_key)
                if actual_size != asset.expected_size:
                    raise ValueError(f"Uploaded size was {actual_size} bytes; expected {asset.expected_size}.")
                asset.confirmed = True
                asset.upload_error = ""
            except Exception as exc:
                asset.confirmed = False
                asset.upload_error = str(exc)[:1000] or "Object was not uploaded."
            asset.save(update_fields=["confirmed", "upload_error", "updated_at"])
            results.append(UploadAssetSerializer(asset).data)
        if any(asset.confirmed for asset in assets):
            dataset.status = Dataset.Status.UPLOADING
            dataset.save(update_fields=["status", "updated_at"])
        return Response({"uploads": results})


class ProcessDatasetView(APIView):
    def post(self, request, dataset_id):
        with transaction.atomic():
            dataset = Dataset.objects.select_for_update().filter(pk=dataset_id, owner=request.user).first()
            if dataset is None:
                raise Http404
            return self._process_locked(request, dataset)

    def _process_locked(self, request, dataset):
        allow_partial = request.data.get("allow_partial", False)
        if not isinstance(allow_partial, bool):
            return error_response("allow_partial must be a boolean.")
        requested_ids = request.data.get("upload_ids")
        if requested_ids is None:
            uploads = dataset.uploads.all()
        elif isinstance(requested_ids, list):
            parsed_ids = parse_uuid_list(requested_ids, "upload_ids")
            uploads = dataset.uploads.filter(pk__in=parsed_ids)
            if uploads.count() != len(parsed_ids):
                return error_response("One or more upload IDs do not belong to this dataset.")
        else:
            return error_response("upload_ids must be a list.")
        upload_rows = list(uploads.order_by("created_at"))
        failed_uploads = [u for u in upload_rows if not u.confirmed]
        confirmed = [u for u in upload_rows if u.confirmed]
        if allow_partial:
            selected_confirmed_ids = [asset.pk for asset in confirmed]
            skipped_elsewhere = dataset.uploads.filter(confirmed=False).exclude(pk__in=selected_confirmed_ids)
            already_listed = {asset.pk for asset in failed_uploads}
            failed_uploads.extend(asset for asset in skipped_elsewhere if asset.pk not in already_listed)
        if failed_uploads and not allow_partial:
            return error_response(
                "Some files did not upload successfully. Retry them or process only confirmed files.",
                code="uploads_incomplete",
                failed_upload_ids=[str(item.pk) for item in failed_uploads],
            )
        if not confirmed:
            return error_response("No confirmed uploads are available to process.")

        active = dataset.jobs.filter(kind=Job.Kind.INGEST_DATASET, status__in=Job.ACTIVE_STATUSES).first()
        if active:
            return Response({"job": JobSerializer(active).data}, status=status.HTTP_202_ACCEPTED)

        skipped_errors = [
            {"upload_id": str(item.pk), "path": item.relative_path, "error": item.upload_error or "Upload was not confirmed."}
            for item in failed_uploads
        ]
        job = Job.objects.create(
            dataset=dataset,
            kind=Job.Kind.INGEST_DATASET,
            status=Job.Status.QUEUED,
            total_count=len(confirmed),
            input_data={
                "upload_asset_ids": [str(asset.pk) for asset in confirmed],
                "allow_partial": allow_partial,
            },
            errors=skipped_errors,
        )
        dataset.status = Dataset.Status.PROCESSING
        dataset.save(update_fields=["status", "updated_at"])
        transaction.on_commit(lambda: enqueue_job(job.pk, Job.Kind.INGEST_DATASET))
        return Response({"job": JobSerializer(job).data}, status=status.HTTP_202_ACCEPTED)


class DatasetImagesView(APIView):
    def get(self, request, dataset_id):
        dataset = get_owned_dataset(request.user, dataset_id)
        queryset = dataset.images.order_by("relative_path")
        status_filter = request.query_params.get("status")
        if status_filter:
            if status_filter not in Image.Status.values:
                return error_response("Invalid image status filter.")
            queryset = queryset.filter(status=status_filter)
        if request.query_params.get("has_location") in {"true", "false"}:
            queryset = queryset.filter(location__isnull=request.query_params.get("has_location") == "false")
        page = StandardPagination()
        return page.get_paginated_response(ImageSerializer(page.paginate_queryset(queryset, request), many=True).data)


class DatasetMapView(APIView):
    def get(self, request, dataset_id):
        dataset = get_owned_dataset(request.user, dataset_id)
        features = []
        for image in dataset.images.filter(location__isnull=False).only(
            "id", "relative_path", "captured_at", "camera_make", "camera_model", "location", "thumbnail_key"
        ):
            features.append(
                {
                    "type": "Feature",
                    "id": str(image.pk),
                    "geometry": {"type": "Point", "coordinates": [image.location.x, image.location.y]},
                    "properties": {
                        "id": str(image.pk),
                        "relative_path": image.relative_path,
                        "captured_at": image.captured_at.isoformat() if image.captured_at else None,
                        "camera_make": image.camera_make,
                        "camera_model": image.camera_model,
                        "thumbnail_url": None,
                    },
                }
            )
        return Response({"type": "FeatureCollection", "features": features})


class ImageDetailView(APIView):
    def get(self, request, image_id):
        try:
            image = Image.objects.get(pk=image_id, dataset__owner=request.user)
        except (Image.DoesNotExist, ValueError):
            raise Http404
        return Response(ImageSerializer(image).data)

    def delete(self, request, image_id):
        parent_ids = set()
        with transaction.atomic():
            image = Image.objects.filter(pk=image_id, dataset__owner=request.user).only("pk", "dataset_id").first()
            if image is None:
                raise Http404
            parent_ids.update(
                Job.objects.filter(image_id=image_id, parent__isnull=False).values_list("parent_id", flat=True)
            )
            list(Job.objects.select_for_update(of=("self",)).filter(pk__in=parent_ids).order_by("pk"))
            image_jobs = list(Job.objects.select_for_update().filter(image_id=image_id))
            if any(job.status in Job.ACTIVE_STATUSES for job in image_jobs):
                return error_response(
                    "Wait for this image to finish processing before deleting it.",
                    code="processing_active",
                    http_status=status.HTTP_409_CONFLICT,
                )
            image = Image.objects.select_for_update(of=("self",)).filter(pk=image_id, dataset__owner=request.user).first()
            if image is None:
                raise Http404
            parent_ids = {job.parent_id for job in image_jobs if job.parent_id}
            object_keys = _image_object_keys([image])
            direct_assets = list(UploadAsset.objects.filter(dataset_id=image.dataset_id, object_key=image.object_key))
            for asset in direct_assets:
                referenced_elsewhere = False
                for parent_input in Job.objects.filter(
                    dataset_id=image.dataset_id,
                    kind=Job.Kind.INGEST_DATASET,
                ).exclude(pk__in=parent_ids).values_list("input_data", flat=True):
                    asset_ids = parent_input.get("upload_asset_ids", []) if isinstance(parent_input, dict) else []
                    if isinstance(asset_ids, list) and str(asset.pk) in {str(value) for value in asset_ids}:
                        referenced_elsewhere = True
                        break
                if not referenced_elsewhere:
                    asset.delete()
            image.delete()
            from processing.tasks import _refresh_parent

            for parent_id in parent_ids:
                parent = Job.objects.select_for_update().filter(pk=parent_id).first()
                if parent:
                    parent.total_count = max(0, parent.total_count - 1)
                    result_data = parent.result_data or {}
                    if isinstance(result_data.get("images_created"), int):
                        result_data["images_created"] = max(0, result_data["images_created"] - 1)
                    if isinstance(result_data.get("images_total"), int):
                        result_data["images_total"] = max(0, result_data["images_total"] - 1)
                    parent.result_data = result_data
                    parent.save(update_fields=["total_count", "result_data", "updated_at"])
                    _refresh_parent(parent_id)
            _refresh_dataset_status(image.dataset_id)
            _delete_objects_after_commit(object_keys)
        return Response(status=status.HTTP_204_NO_CONTENT)


class JobListCreateView(APIView):
    def get(self, request):
        queryset = Job.objects.filter(dataset__owner=request.user).select_related("dataset", "image")
        for param, field, allowed in (
            ("status", "status", Job.Status.values),
            ("type", "kind", Job.Kind.values),
        ):
            value = request.query_params.get(param)
            if value:
                if value not in allowed:
                    return error_response(f"Invalid {param} filter.")
                queryset = queryset.filter(**{field: value})
        for param, field in (("dataset", "dataset_id"), ("parent_job", "parent_id")):
            value = request.query_params.get(param)
            if value:
                try:
                    parsed = uuid.UUID(value)
                except (ValueError, AttributeError):
                    return error_response(f"Invalid {param} filter.")
                queryset = queryset.filter(**{field: parsed})
        page = JobPagination()
        return page.get_paginated_response(JobSerializer(page.paginate_queryset(queryset, request), many=True).data)

    def post(self, request):
        kind = request.data.get("kind")
        image_id = request.data.get("image_id")
        if not isinstance(kind, str) or kind not in {Job.Kind.EXTRACT_METADATA, Job.Kind.GENERATE_THUMBNAIL}:
            return error_response("kind must be extract_metadata or generate_thumbnail.")
        try:
            image_id = uuid.UUID(str(image_id))
        except (ValueError, TypeError, AttributeError):
            return error_response("image_id must be a valid UUID.")
        try:
            image = Image.objects.select_related("dataset").get(pk=image_id, dataset__owner=request.user)
        except (Image.DoesNotExist, ValueError):
            raise Http404
        parent_id = request.data.get("parent_id")
        parent = None
        if parent_id is not None:
            try:
                parent_id = uuid.UUID(str(parent_id))
            except (ValueError, TypeError, AttributeError):
                return error_response("parent_id must be a valid UUID.")
            parent = Job.objects.filter(
                pk=parent_id,
                dataset=image.dataset,
                kind=Job.Kind.INGEST_DATASET,
            ).first()
            if parent is None:
                return error_response("parent_id must identify an upload session for this dataset.")
        else:
            previous_child = Job.objects.filter(
                image=image,
                kind=kind,
                parent__kind=Job.Kind.INGEST_DATASET,
            ).select_related("parent").order_by("-created_at").first()
            parent = previous_child.parent if previous_child else None
        if parent and parent.status == Job.Status.CANCELLED:
            return error_response(
                "This upload session was cancelled. Start a new upload session to process its remaining files.",
                code="processing_cancelled",
                http_status=status.HTTP_409_CONFLICT,
            )
        existing = Job.objects.filter(image=image, kind=kind, status__in=Job.ACTIVE_STATUSES).first()
        if existing:
            return Response({"job": JobSerializer(existing).data}, status=status.HTTP_202_ACCEPTED)
        try:
            with transaction.atomic():
                if parent:
                    parent.status = Job.Status.RUNNING
                    parent.finished_at = None
                    parent.save(update_fields=["status", "finished_at", "updated_at"])
                    Dataset.objects.filter(pk=image.dataset_id).update(
                        status=Dataset.Status.PROCESSING,
                        updated_at=timezone.now(),
                    )
                job = Job.objects.create(
                    dataset=image.dataset,
                    image=image,
                    parent=parent,
                    kind=kind,
                    status=Job.Status.QUEUED,
                    total_count=1,
                    input_data={"image_id": str(image.pk)},
                )
                transaction.on_commit(lambda: enqueue_job(job.pk, kind))
        except IntegrityError:
            job = Job.objects.get(image=image, kind=kind, status__in=Job.ACTIVE_STATUSES)
            return Response({"job": JobSerializer(job).data}, status=status.HTTP_202_ACCEPTED)
        return Response({"job": JobSerializer(job).data}, status=status.HTTP_202_ACCEPTED)


class JobDetailView(APIView):
    def get(self, request, job_id):
        try:
            job = Job.objects.select_related("dataset", "image").get(pk=job_id, dataset__owner=request.user)
        except (Job.DoesNotExist, ValueError):
            raise Http404
        return Response(JobSerializer(job).data)

    def delete(self, request, job_id):
        with transaction.atomic():
            parent = Job.objects.select_for_update(of=("self",)).filter(pk=job_id, dataset__owner=request.user).first()
            if parent is None:
                raise Http404
            if parent.kind != Job.Kind.INGEST_DATASET:
                return error_response("Only upload sessions can be deleted.")
            child_jobs = list(Job.objects.select_for_update(of=("self",)).filter(parent=parent).only("status").order_by("pk"))
            related_image_ids = list(Image.objects.filter(jobs__parent=parent).values_list("pk", flat=True).distinct())
            related_images = list(Image.objects.select_for_update().filter(pk__in=related_image_ids).order_by("pk"))
            if parent.status in Job.ACTIVE_STATUSES or any(job.status in Job.ACTIVE_STATUSES for job in child_jobs):
                return error_response(
                    "Wait for processing to finish before deleting this upload session.",
                    code="processing_active",
                    http_status=status.HTTP_409_CONFLICT,
                )

            dataset_id = parent.dataset_id
            related_image_ids = [image.pk for image in related_images]
            images_used_elsewhere = set(
                Job.objects.filter(image_id__in=related_image_ids)
                .exclude(parent=parent)
                .values_list("image_id", flat=True)
                .distinct()
            )
            images_to_delete = [image for image in related_images if image.pk not in images_used_elsewhere]
            object_keys = _image_object_keys(images_to_delete)

            requested_asset_ids = (parent.input_data or {}).get("upload_asset_ids", [])
            if not isinstance(requested_asset_ids, list):
                requested_asset_ids = []
            other_asset_ids = set()
            for other_input in Job.objects.filter(dataset_id=dataset_id, kind=Job.Kind.INGEST_DATASET).exclude(pk=parent.pk).values_list("input_data", flat=True):
                ids = other_input.get("upload_asset_ids", []) if isinstance(other_input, dict) else []
                if isinstance(ids, list):
                    other_asset_ids.update(str(asset_id) for asset_id in ids)
            assets_to_delete = UploadAsset.objects.filter(dataset_id=dataset_id, pk__in=requested_asset_ids)
            assets_to_delete = [asset for asset in assets_to_delete if str(asset.pk) not in other_asset_ids]
            object_keys.extend(asset.object_key for asset in assets_to_delete)

            for image in images_to_delete:
                image.delete()
            for asset in assets_to_delete:
                asset.delete()
            parent.delete()
            _refresh_dataset_status(dataset_id)
            _delete_objects_after_commit(object_keys)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CancelUploadSessionView(APIView):
    def post(self, request, job_id):
        with transaction.atomic():
            parent = Job.objects.select_for_update(of=("self",)).filter(pk=job_id, dataset__owner=request.user).first()
            if parent is None:
                raise Http404
            if parent.kind != Job.Kind.INGEST_DATASET:
                return error_response("Only upload sessions can be cancelled.")
            active_children = list(
                Job.objects.select_for_update(of=("self",))
                .filter(parent_id=parent.pk, status__in=Job.ACTIVE_STATUSES)
                .order_by("pk")
            )
            if parent.status == Job.Status.CANCELLED and not active_children:
                return Response({"cancelled_count": 0, "job": JobSerializer(parent).data})
            if parent.status not in Job.ACTIVE_STATUSES and not active_children:
                return error_response(
                    "This upload session is no longer processing.",
                    code="processing_inactive",
                    http_status=status.HTTP_409_CONFLICT,
                )

            now = timezone.now()
            child_ids = [child.pk for child in active_children]
            image_ids = {child.image_id for child in active_children if child.image_id}
            if child_ids:
                Job.objects.filter(pk__in=child_ids).update(
                    status=Job.Status.CANCELLED,
                    finished_at=now,
                    updated_at=now,
                )

            # A task in another session may still be processing the same image.
            # Only reset images for which this cancellation removed the last
            # active task, then let the normal image state aggregation decide
            # whether the image is pending, failed, or already complete.
            still_active_images = set(
                Job.objects.filter(image_id__in=image_ids, status__in=Job.ACTIVE_STATUSES)
                .values_list("image_id", flat=True)
            )
            from processing.tasks import _refresh_image

            for image_id in image_ids - still_active_images:
                _refresh_image(image_id)

            parent.status = Job.Status.CANCELLED
            parent.finished_at = now
            parent.save(update_fields=["status", "finished_at", "updated_at"])
            _refresh_dataset_status(parent.dataset_id)

            task_ids = [str(parent.pk), *(str(child_id) for child_id in child_ids)]
            transaction.on_commit(lambda: revoke_jobs(task_ids))

        return Response({
            "cancelled_count": len(child_ids) + 1,
            "job": JobSerializer(parent).data,
        })


class RetryFailedBatchView(APIView):
    def post(self, request, job_id):
        with transaction.atomic():
            parent = Job.objects.select_for_update(of=("self",)).filter(pk=job_id, dataset__owner=request.user).first()
            if parent is None:
                raise Http404
            if parent.kind != Job.Kind.INGEST_DATASET:
                return error_response("This job is not an upload session.")
            if parent.status == Job.Status.CANCELLED:
                return error_response(
                    "This upload session was cancelled. Start a new upload session to process its remaining files.",
                    code="processing_cancelled",
                    http_status=status.HTTP_409_CONFLICT,
                )

            image_ids = list(Job.objects.filter(parent=parent, image__isnull=False).values_list("image_id", flat=True).distinct())
            list(Image.objects.select_for_update(of=("self",)).filter(pk__in=image_ids).order_by("pk"))
            latest_by_image_task = {}
            tasks = Job.objects.filter(
                image_id__in=image_ids,
                kind__in=[Job.Kind.EXTRACT_METADATA, Job.Kind.GENERATE_THUMBNAIL],
            ).order_by("-created_at", "-pk")
            for task in tasks:
                latest_by_image_task.setdefault((task.image_id, task.kind), task)
            failed_tasks = [
                task for task in latest_by_image_task.values()
                if task.parent_id == parent.pk and task.status == Job.Status.FAILED
            ]
            if not failed_tasks:
                return Response({"retried_count": 0}, status=status.HTTP_200_OK)

            created_jobs = []
            affected_image_ids = set()
            for failed in failed_tasks:
                new_job = Job.objects.create(
                    dataset_id=parent.dataset_id,
                    image_id=failed.image_id,
                    parent=parent,
                    kind=failed.kind,
                    status=Job.Status.QUEUED,
                    total_count=1,
                    input_data=failed.input_data or {"image_id": str(failed.image_id)},
                )
                created_jobs.append((new_job.pk, new_job.kind))
                affected_image_ids.add(failed.image_id)

            now = timezone.now()
            Image.objects.filter(pk__in=affected_image_ids).update(status=Image.Status.PROCESSING, updated_at=now)
            parent.status = Job.Status.RUNNING
            parent.finished_at = None
            parent.save(update_fields=["status", "finished_at", "updated_at"])
            Dataset.objects.filter(pk=parent.dataset_id).update(status=Dataset.Status.PROCESSING, updated_at=now)
            from processing.tasks import _refresh_parent

            _refresh_parent(parent.pk)
            transaction.on_commit(lambda: [enqueue_job(job_id, kind) for job_id, kind in created_jobs])

        return Response({"retried_count": len(created_jobs)}, status=status.HTTP_202_ACCEPTED)
