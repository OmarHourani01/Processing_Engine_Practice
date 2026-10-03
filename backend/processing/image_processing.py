"""EXIF extraction and browser-friendly WebP derivatives."""

from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone
from io import BytesIO
import unicodedata

from django.utils import timezone
from PIL import ExifTags, Image as PILImage, ImageFile, ImageOps, UnidentifiedImageError

from .exceptions import InvalidImage

MAX_PIXELS = 100_000_000
THUMBNAIL_SIZE = (320, 320)
PREVIEW_SIZE = (1600, 1600)
ImageFile.LOAD_TRUNCATED_IMAGES = False


def _rational(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise InvalidImage("Image GPS metadata is malformed") from exc


def _dms_to_decimal(value, reference) -> float:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise InvalidImage("Image GPS metadata is malformed")
    degrees, minutes, seconds = (_rational(part) for part in value)
    decimal = degrees + minutes / 60 + seconds / 3600
    if isinstance(reference, bytes):
        reference = reference.decode("ascii", errors="ignore")
    if str(reference).upper() in {"S", "W"}:
        decimal *= -1
    return decimal


def _exif_value(exif, tag_name, default=None):
    tag_id = next((number for number, name in ExifTags.TAGS.items() if name == tag_name), None)
    return exif.get(tag_id, default) if tag_id is not None else default


def _clean_exif_text(value, max_length: int = 160) -> str | None:
    """Return printable, database-safe EXIF text within the model field limit."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")

    # Some cameras, including the DJI XT2, store fixed-width strings padded
    # with NUL bytes. PostgreSQL text fields reject NUL characters entirely.
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(character for character in text if character.isprintable())
    text = " ".join(text.split())
    return text[:max_length] or None


def _read_gps(exif):
    gps_id = getattr(ExifTags, "IFD", None)
    gps_id = getattr(gps_id, "GPSInfo", 34853)
    try:
        gps = exif.get_ifd(gps_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        gps = exif.get(34853, {})
    if not isinstance(gps, dict) or not gps:
        return None

    names = {number: name for number, name in ExifTags.GPSTAGS.items()}
    by_name = {names.get(number, number): value for number, value in gps.items()}
    lat, lat_ref = by_name.get("GPSLatitude"), by_name.get("GPSLatitudeRef")
    lon, lon_ref = by_name.get("GPSLongitude"), by_name.get("GPSLongitudeRef")
    if lat is None or lon is None or lat_ref is None or lon_ref is None:
        return None
    latitude = _dms_to_decimal(lat, lat_ref)
    longitude = _dms_to_decimal(lon, lon_ref)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise InvalidImage("Image GPS coordinates are outside valid ranges")
    return longitude, latitude


def _capture_time(exif):
    raw = _exif_value(exif, "DateTimeOriginal") or _exif_value(exif, "DateTimeDigitized")
    if not raw:
        return None
    try:
        parsed = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    # EXIF timestamps usually lack an offset. Interpret those values as UTC so
    # the API can return a stable timezone-aware value.
    return timezone.make_aware(parsed, datetime_timezone.utc)


def inspect_image(file_obj) -> dict:
    """Verify the image and return the fields stored on `datasets.Image`."""
    try:
        file_obj.seek(0)
        with PILImage.open(file_obj) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise InvalidImage("Image dimensions exceed the processing limit")
            exif = image.getexif()
            gps = _read_gps(exif)
            # Verify the compressed data without retaining a decoded raster.
            image.verify()
        return {
            "width": width,
            "height": height,
            "captured_at": _capture_time(exif),
            "camera_make": _clean_exif_text(_exif_value(exif, "Make", "")),
            "camera_model": _clean_exif_text(_exif_value(exif, "Model", "")),
            "gps": gps,
        }
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, PILImage.DecompressionBombError) as exc:
        raise InvalidImage("The file is not a valid supported image") from exc


def _webp_bytes(image: PILImage.Image, max_size: tuple[int, int]) -> bytes:
    image.thumbnail(max_size, PILImage.Resampling.LANCZOS)
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    output = BytesIO()
    image.save(output, format="WEBP", quality=82, method=5)
    return output.getvalue()


def make_derivatives(file_obj) -> tuple[bytes, bytes]:
    """Decode an image and return `(thumbnail.webp, preview.webp)` bytes."""
    try:
        file_obj.seek(0)
        with PILImage.open(file_obj) as source:
            if source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_PIXELS:
                raise InvalidImage("Image dimensions exceed the processing limit")
            source.seek(0)
            oriented = ImageOps.exif_transpose(source)
            oriented.load()
            preview = _webp_bytes(oriented.copy(), PREVIEW_SIZE)
            thumbnail = _webp_bytes(oriented.copy(), THUMBNAIL_SIZE)
            return thumbnail, preview
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, PILImage.DecompressionBombError) as exc:
        raise InvalidImage("The file could not be decoded as a supported image") from exc
