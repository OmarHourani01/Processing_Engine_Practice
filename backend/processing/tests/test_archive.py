from io import BytesIO
from unittest import TestCase
from zipfile import ZIP_DEFLATED, ZipFile

from processing.archive import validate_zip_archive
from processing.exceptions import InvalidDataset


def make_archive(files):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    buffer.seek(0)
    return buffer


class ZipValidationTests(TestCase):
    def test_validates_all_members_before_import(self):
        seen = []

        def validate_image(file_obj):
            seen.append(file_obj.read())

        manifest = validate_zip_archive(
            make_archive({"photos/a.jpg": b"image-data", "README.txt": b"notes"}),
            image_validator=validate_image,
        )

        self.assertEqual(manifest.file_count, 2)
        self.assertEqual(manifest.expanded_bytes, 15)
        self.assertEqual(manifest.image_count, 1)
        self.assertEqual(seen, [b"image-data"])

    def test_rejects_path_traversal_before_import(self):
        with self.assertRaises(InvalidDataset):
            validate_zip_archive(make_archive({"safe.jpg": b"ok", "../escape.jpg": b"bad"}))

    def test_rejects_duplicate_normalized_paths(self):
        archive = BytesIO()
        with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
            zip_file.writestr("photos/same.jpg", b"first")
            zip_file.writestr("photos/./same.jpg", b"second")
        archive.seek(0)

        with self.assertRaises(InvalidDataset):
            validate_zip_archive(archive)

    def test_rejects_unsupported_entry_count_over_limit(self):
        from processing.archive import MAX_IMAGES

        files = {f"notes/{index}.txt": b"x" for index in range(MAX_IMAGES + 1)}
        with self.assertRaises(InvalidDataset):
            validate_zip_archive(make_archive(files))
