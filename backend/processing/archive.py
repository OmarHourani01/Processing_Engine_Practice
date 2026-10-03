"""Safe, bounded ZIP iteration for dataset uploads."""

from __future__ import annotations

import hashlib
import lzma
import mimetypes
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

from django.conf import settings

from .exceptions import InvalidDataset

MAX_IMAGES = settings.UPLOAD_MAX_FILES
MAX_TOTAL_BYTES = settings.UPLOAD_MAX_BYTES
MAX_IMAGE_BYTES = MAX_TOTAL_BYTES
MAX_SIZE_LABEL = (
    f"{MAX_TOTAL_BYTES / (1024 ** 3):g} GB"
    if MAX_TOTAL_BYTES % (1024 ** 3) == 0
    else f"{MAX_TOTAL_BYTES:,} bytes"
)
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".geotiff"}


@dataclass
class ArchiveEntry:
    relative_path: str
    object_key: str
    content_type: str
    size: int
    data: BinaryIO


@dataclass
class ArchiveManifest:
    file_count: int
    expanded_bytes: int
    image_count: int
    invalid_images: list[tuple[str, str]]
    paths: list[str]


def safe_relative_path(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise InvalidDataset("ZIP contains an unsafe path")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidDataset("ZIP contains an unsafe path")
    if path.parts and path.parts[0].endswith(":"):
        raise InvalidDataset("ZIP contains an unsafe path")
    return unicodedata.normalize("NFC", path.as_posix())


def is_supported_image(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in SUPPORTED_EXTENSIONS


def validate_zip_archive(archive: BinaryIO, image_validator=None) -> ArchiveManifest:
    """Validate every member before ingestion writes rows or objects.

    The archive is streamed to disk-backed buffers, never extracted by its
    stored path. Reading every entry also verifies its CRC before import.
    `image_validator` can inspect dimensions/decodability; invalid image bytes
    are reported per file so their child jobs can surface terminal errors.
    """
    try:
        archive.seek(0)
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvalidDataset("The uploaded ZIP file is invalid") from exc

    file_count = 0
    expanded_bytes = 0
    image_count = 0
    invalid_images = []
    paths = []
    try:
        with zf:
            entries = []
            seen_paths = set()
            for info in zf.infolist():
                relative_path = safe_relative_path(info.filename.rstrip("/")) if info.is_dir() else safe_relative_path(info.filename)
                if info.is_dir():
                    continue
                if relative_path in seen_paths:
                    raise InvalidDataset(f"ZIP contains duplicate normalized path: {relative_path}")
                seen_paths.add(relative_path)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise InvalidDataset("ZIP symlinks are not allowed")
                if info.flag_bits & 0x1:
                    raise InvalidDataset("Encrypted ZIP entries are not supported")
                if info.file_size < 0 or info.file_size > MAX_IMAGE_BYTES:
                    raise InvalidDataset(f"A ZIP entry exceeds the {MAX_SIZE_LABEL} image limit")
                file_count += 1
                expanded_bytes += info.file_size
                if file_count > MAX_IMAGES:
                    raise InvalidDataset("A dataset may contain at most 1,000 files")
                if expanded_bytes > MAX_TOTAL_BYTES:
                    raise InvalidDataset(f"Expanded dataset size exceeds {MAX_SIZE_LABEL}")
                image_count += int(is_supported_image(relative_path))
                paths.append(relative_path)
                entries.append((info, relative_path))

            observed_total = 0
            for info, relative_path in entries:
                image_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
                observed_size = 0
                try:
                    with zf.open(info, "r") as source:
                        while chunk := source.read(1024 * 1024):
                            observed_size += len(chunk)
                            observed_total += len(chunk)
                            if observed_size > MAX_IMAGE_BYTES or observed_total > MAX_TOTAL_BYTES:
                                raise InvalidDataset(f"Expanded dataset size exceeds {MAX_SIZE_LABEL}")
                            if is_supported_image(relative_path):
                                image_file.write(chunk)
                    if observed_size != info.file_size:
                        raise InvalidDataset("ZIP entry size did not match its directory record")
                    if is_supported_image(relative_path) and image_validator:
                        image_file.seek(0)
                        try:
                            image_validator(image_file)
                        except InvalidDataset:
                            raise
                        except Exception as exc:
                            # Keep malformed images available as individual
                            # failed images after the safe archive is imported.
                            invalid_images.append((relative_path, str(exc)[:500]))
                finally:
                    image_file.close()
    except InvalidDataset:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError, zlib.error, lzma.LZMAError) as exc:
        raise InvalidDataset("A ZIP entry is corrupt or could not be read") from exc
    return ArchiveManifest(file_count, observed_total, image_count, invalid_images, paths)


def object_key_for(dataset_id: str, relative_path: str, source_id: str | None = None) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    source_segment = f"/{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:20]}" if source_id else ""
    return f"datasets/{dataset_id}/images{source_segment}/{digest}{suffix}"


def iter_zip_images(archive: BinaryIO, dataset_id: str, source_id: str | None = None) -> Iterator[ArchiveEntry]:
    """Yield supported image files through bounded disk-backed streams.

    The archive is never extracted to a filesystem path. All files are checked
    before reading, and the declared and observed expanded bytes are bounded.
    Caller owns and closes each yielded entry's `data` stream.
    """
    import tempfile

    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvalidDataset("The uploaded ZIP file is invalid") from exc

    with zf:
        files = []
        declared_total = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            relative_path = safe_relative_path(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise InvalidDataset("ZIP symlinks are not allowed")
            if info.flag_bits & 0x1:
                raise InvalidDataset("Encrypted ZIP entries are not supported")
            if info.file_size < 0 or info.file_size > MAX_IMAGE_BYTES:
                raise InvalidDataset(f"A ZIP entry exceeds the {MAX_SIZE_LABEL} image limit")
            declared_total += info.file_size
            files.append((info, relative_path))
            if len(files) > MAX_IMAGES:
                raise InvalidDataset("A dataset may contain at most 1,000 files")
            if declared_total > MAX_TOTAL_BYTES:
                raise InvalidDataset(f"Expanded dataset size exceeds {MAX_SIZE_LABEL}")

        total_observed = 0
        yielded_images = 0
        for info, relative_path in files:
            if not is_supported_image(relative_path):
                continue
            yielded_images += 1
            if yielded_images > MAX_IMAGES:
                raise InvalidDataset("A dataset may contain at most 1,000 images")
            target = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
            observed_size = 0
            try:
                with zf.open(info, "r") as source:
                    while chunk := source.read(1024 * 1024):
                        observed_size += len(chunk)
                        total_observed += len(chunk)
                        if observed_size > MAX_IMAGE_BYTES or total_observed > MAX_TOTAL_BYTES:
                            raise InvalidDataset(f"Expanded dataset size exceeds {MAX_SIZE_LABEL}")
                        target.write(chunk)
                if observed_size != info.file_size:
                    raise InvalidDataset("ZIP entry size did not match its directory record")
                target.seek(0)
                content_type = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
                yield ArchiveEntry(
                    relative_path=relative_path,
                    object_key=object_key_for(dataset_id, relative_path, source_id),
                    content_type=content_type,
                    size=observed_size,
                    data=target,
                )
            except Exception:
                target.close()
                raise
