from io import BytesIO

from django.test import SimpleTestCase
from PIL import Image as PILImage

from processing.exceptions import InvalidImage
from processing.image_processing import inspect_image, make_derivatives


def make_jpeg_with_exif():
    output = BytesIO()
    image = PILImage.new("RGB", (64, 32), color=(20, 80, 140))
    exif = PILImage.Exif()
    exif[271] = "Example Camera Co."
    exif[272] = "Model 1"
    exif[36867] = "2025:02:03 04:05:06"
    image.save(output, format="JPEG", exif=exif)
    output.seek(0)
    return output


class ImageProcessingTests(SimpleTestCase):
    def test_extracts_dimensions_camera_and_capture_time(self):
        metadata = inspect_image(make_jpeg_with_exif())

        self.assertEqual((metadata["width"], metadata["height"]), (64, 32))
        self.assertEqual(metadata["camera_make"], "Example Camera Co.")
        self.assertEqual(metadata["camera_model"], "Model 1")
        self.assertEqual(metadata["captured_at"].isoformat(), "2025-02-03T04:05:06+00:00")
        self.assertIsNone(metadata["gps"])

    def test_generates_small_webp_thumbnail_and_preview(self):
        thumbnail, preview = make_derivatives(make_jpeg_with_exif())

        for content in (thumbnail, preview):
            with PILImage.open(BytesIO(content)) as generated:
                self.assertEqual(generated.format, "WEBP")
                self.assertEqual(generated.size, (64, 32))

    def test_invalid_image_is_terminal(self):
        with self.assertRaises(InvalidImage):
            inspect_image(BytesIO(b"not an image"))

