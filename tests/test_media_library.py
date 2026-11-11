import os
import tempfile
import unittest
from unittest.mock import patch

import media_library


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MEDIA_LIBRARY_DIR": self.temp_dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def write_file(self, name, contents=b"media"):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as file:
            file.write(contents)
        return path

    def test_lists_videos_in_natural_order_with_sidecar_thumbnail(self):
        self.write_file("Episode 10.mp4")
        self.write_file("Episode 2.mp4")
        self.write_file("Episode 2.jpg", b"thumbnail")
        self.write_file("notes.txt")

        items = media_library.list_media()

        self.assertEqual([item["name"] for item in items], ["Episode 2.mp4", "Episode 10.mp4"])
        self.assertTrue(items[0]["has_thumbnail"])
        self.assertFalse(items[1]["has_thumbnail"])
        self.assertTrue(os.path.samefile(
            media_library.resolve_thumbnail("Episode 2.mp4"),
            os.path.join(self.temp_dir.name, "Episode 2.jpg"),
        ))

    def test_rejects_traversal_and_unsupported_media(self):
        self.write_file("video.mp4")

        for invalid_name in ("../video.mp4", "..\\video.mp4", "notes.txt"):
            with self.subTest(name=invalid_name):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    media_library.resolve_episode(invalid_name)

    def test_rejects_symlink_outside_library(self):
        outside = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        outside.close()
        link_path = os.path.join(self.temp_dir.name, "outside.mp4")
        try:
            os.symlink(outside.name, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable.")
        try:
            with self.assertRaises(ValueError):
                media_library.resolve_episode("outside.mp4")
        finally:
            os.unlink(outside.name)


if __name__ == "__main__":
    unittest.main()
