from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
import logging

logger = logging.getLogger(__name__)
SINGLE_POST_MAX_BYTES = 5_000_000_000
MULTIPART_PART_SIZE = 64 * 1024 * 1024


@lru_cache(maxsize=2)
def s3_client(public=False):
    return boto3.client(
        "s3",
        endpoint_url=settings.MINIO_PUBLIC_ENDPOINT_URL if public else settings.MINIO_ENDPOINT_URL,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        region_name=settings.MINIO_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_bucket_and_cors():
    client = s3_client()
    try:
        client.head_bucket(Bucket=settings.MINIO_BUCKET)
    except client.exceptions.NoSuchBucket:
        client.create_bucket(Bucket=settings.MINIO_BUCKET)
    except Exception:
        # Some S3-compatible servers use generic 404 responses instead of
        # the NoSuchBucket error shape. CreateBucket is idempotent in MinIO.
        try:
            client.create_bucket(Bucket=settings.MINIO_BUCKET)
        except Exception:
            client.head_bucket(Bucket=settings.MINIO_BUCKET)
    origins = settings.MINIO_ALLOWED_ORIGINS or ["http://localhost:5173"]
    try:
        client.put_bucket_cors(
            Bucket=settings.MINIO_BUCKET,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": origins,
                        "AllowedMethods": ["GET", "HEAD", "POST", "PUT", "DELETE"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        )
    except ClientError as exc:
        # MinIO Community Edition does not implement PutBucketCors; it enables
        # CORS by default for every bucket. Other S3-compatible stores should
        # still apply the explicit rules, and unexpected errors must surface.
        if exc.response.get("Error", {}).get("Code") != "NotImplemented":
            raise
        logger.info("Object store does not support PutBucketCors; using its default CORS behavior.")


def presigned_upload(object_key, content_type, max_size):
    client = s3_client(public=True)
    return client.generate_presigned_post(
        Bucket=settings.MINIO_BUCKET,
        Key=object_key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, max_size],
            ["eq", "$key", object_key],
        ],
        ExpiresIn=settings.UPLOAD_URL_TTL_SECONDS,
    )


def presigned_multipart_upload(object_key, content_type, size):
    client = s3_client()
    upload_id = client.create_multipart_upload(
        Bucket=settings.MINIO_BUCKET,
        Key=object_key,
        ContentType=content_type,
    )["UploadId"]
    part_count = (size + MULTIPART_PART_SIZE - 1) // MULTIPART_PART_SIZE
    public_client = s3_client(public=True)
    try:
        parts = [
            {
                "part_number": part_number,
                "upload_url": public_client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": settings.MINIO_BUCKET,
                        "Key": object_key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=settings.UPLOAD_URL_TTL_SECONDS,
                ),
            }
            for part_number in range(1, part_count + 1)
        ]
        abort_url = public_client.generate_presigned_url(
            "abort_multipart_upload",
            Params={"Bucket": settings.MINIO_BUCKET, "Key": object_key, "UploadId": upload_id},
            ExpiresIn=settings.UPLOAD_URL_TTL_SECONDS,
        )
    except Exception:
        client.abort_multipart_upload(Bucket=settings.MINIO_BUCKET, Key=object_key, UploadId=upload_id)
        raise
    return {"upload_id": upload_id, "part_size": MULTIPART_PART_SIZE, "parts": parts, "abort_url": abort_url}


def complete_multipart_upload(object_key, upload_id, expected_size):
    client = s3_client()
    parts = []
    marker = 0
    uploaded_size = 0
    while True:
        response = client.list_parts(
            Bucket=settings.MINIO_BUCKET,
            Key=object_key,
            UploadId=upload_id,
            PartNumberMarker=marker,
        )
        for part in response.get("Parts", []):
            parts.append({"PartNumber": part["PartNumber"], "ETag": part["ETag"]})
            uploaded_size += part["Size"]
            marker = part["PartNumber"]
        if not response.get("IsTruncated"):
            break
    expected_part_count = (expected_size + MULTIPART_PART_SIZE - 1) // MULTIPART_PART_SIZE
    if len(parts) != expected_part_count or uploaded_size != expected_size:
        raise ValueError("The uploaded file parts do not match the expected file size.")
    if [part["PartNumber"] for part in parts] != list(range(1, expected_part_count + 1)):
        raise ValueError("The uploaded file is missing one or more parts.")
    return client.complete_multipart_upload(
        Bucket=settings.MINIO_BUCKET,
        Key=object_key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def object_size(object_key):
    response = s3_client().head_object(Bucket=settings.MINIO_BUCKET, Key=object_key)
    return response["ContentLength"]


def delete_objects(object_keys):
    keys = tuple(dict.fromkeys(key for key in object_keys if key))
    client = s3_client()
    for start in range(0, len(keys), 1000):
        batch = keys[start:start + 1000]
        try:
            response = client.delete_objects(
                Bucket=settings.MINIO_BUCKET,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors = response.get("Errors", [])
            if errors:
                raise RuntimeError(f"Object store rejected deletion of {len(errors)} object(s): {errors[0]}")
        except Exception:
            logger.exception("Could not remove %d stored object(s)", len(batch))
            raise


def schedule_object_deletions(object_keys):
    """Persist object keys for deletion and enqueue the cleanup worker."""
    from .models import StoredObjectDeletion
    from .tasks_dispatch import enqueue_storage_cleanup

    keys = tuple(dict.fromkeys(key for key in object_keys if key))
    if not keys:
        return
    StoredObjectDeletion.objects.bulk_create(
        [StoredObjectDeletion(object_key=key) for key in keys], ignore_conflicts=True
    )
    from django.db import transaction

    transaction.on_commit(enqueue_storage_cleanup)


def signed_object_url(object_key, ttl=None):
    if not object_key:
        return None
    return s3_client(public=True).generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.MINIO_BUCKET, "Key": object_key},
        ExpiresIn=ttl or settings.MEDIA_URL_TTL_SECONDS,
    )
