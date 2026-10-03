from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase

from processing.exceptions import InvalidImage
from processing.storage import download_to_file


class DownloadToFileTests(SimpleTestCase):
    def test_caller_validation_error_is_not_rewritten_as_storage_failure(self):
        body = BytesIO(b"image bytes")
        client = type("S3Client", (), {"get_object": lambda *_args, **_kwargs: {"Body": body}})()

        with patch("processing.storage.minio_client", return_value=client):
            with self.assertRaises(InvalidImage):
                with download_to_file("image.jpg") as image_file:
                    self.assertEqual(image_file.read(), b"image bytes")
                    raise InvalidImage("The file is not a valid supported image")

        self.assertTrue(body.closed)
