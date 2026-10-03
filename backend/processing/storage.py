"""Private MinIO access used by ingestion and image workers.

MinIO exposes an S3-compatible API, so workers use the same boto3 client as
the Django API and its presigned upload/download URLs.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import BinaryIO, Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .exceptions import MissingObject, RetryableProcessingError


def minio_client():
    endpoint = os.environ.get("MINIO_ENDPOINT_URL")
    if not endpoint:
        host = os.environ.get("MINIO_ENDPOINT", "minio:9000")
        secure = os.environ.get("MINIO_SECURE", "0").lower() in {"1", "true", "yes"}
        endpoint = f"{'https' if secure else 'http'}://{host}"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get(
            "MINIO_ACCESS_KEY", os.environ.get("MINIO_ROOT_USER", "local-minio")
        ),
        aws_secret_access_key=os.environ.get(
            "MINIO_SECRET_KEY", os.environ.get("MINIO_ROOT_PASSWORD", "local-minio-password")
        ),
        region_name=os.environ.get("MINIO_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def bucket_name() -> str:
    return os.environ.get("MINIO_BUCKET", "image-datasets")


def _translate_storage_error(exc: Exception) -> Exception:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "Unknown"))
        if code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NoSuchVersion", "404"}:
            return MissingObject(f"Object is not available in MinIO ({code})")
        return RetryableProcessingError(f"MinIO request failed ({code})")
    if isinstance(exc, BotoCoreError):
        return RetryableProcessingError(f"MinIO connection failed ({type(exc).__name__})")
    if isinstance(exc, OSError):
        return RetryableProcessingError(f"MinIO stream failed ({type(exc).__name__})")
    return RetryableProcessingError(f"MinIO connection failed ({type(exc).__name__})")


def stat_object(object_key: str):
    try:
        return minio_client().head_object(Bucket=bucket_name(), Key=object_key)
    except Exception as exc:
        translated = _translate_storage_error(exc)
        if translated is exc:
            raise
        raise translated from exc


@contextmanager
def download_to_file(object_key: str) -> Iterator[BinaryIO]:
    """Yield a seekable temporary file without keeping large objects in RAM."""
    import tempfile

    client = minio_client()
    response = None
    try:
        try:
            response = client.get_object(Bucket=bucket_name(), Key=object_key)
        except Exception as exc:
            translated = _translate_storage_error(exc)
            if translated is exc:
                raise
            raise translated from exc

        body = response["Body"]
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as target:
            try:
                while chunk := body.read(1024 * 1024):
                    target.write(chunk)
            except Exception as exc:
                translated = _translate_storage_error(exc)
                if translated is exc:
                    raise
                raise translated from exc
            target.seek(0)
            # Keep caller processing outside storage exception translation:
            # InvalidImage/InvalidDataset must remain terminal input errors.
            yield target
    finally:
        if response is not None:
            response["Body"].close()


def upload_file(object_key: str, source: BinaryIO, size: int, content_type: str) -> None:
    try:
        source.seek(0)
        minio_client().put_object(
            Bucket=bucket_name(),
            Key=object_key,
            Body=source,
            ContentLength=size,
            ContentType=content_type,
        )
    except Exception as exc:
        translated = _translate_storage_error(exc)
        if translated is exc:
            raise
        raise translated from exc


def upload_bytes(object_key: str, data: bytes, content_type: str) -> None:
    import io

    upload_file(object_key, io.BytesIO(data), len(data), content_type)
