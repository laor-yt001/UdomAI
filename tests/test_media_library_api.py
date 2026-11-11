import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from web_dashboard import DashboardHandler


class MediaLibraryTestHandler(DashboardHandler):
    authenticated = True

    def get_current_user(self):
        if self.authenticated:
            return {"user_id": "test-user", "status": "APPROVED"}
        return None


class MediaLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MEDIA_LIBRARY_DIR": self.temp_dir.name})
        self.env.start()
        self.write_file("Episode 1.mp4", b"video-data")
        self.write_file("Episode 1.jpg", b"thumbnail-data")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MediaLibraryTestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env.stop()
        self.temp_dir.cleanup()

    def write_file(self, name, contents):
        with open(os.path.join(self.temp_dir.name, name), "wb") as file:
            file.write(contents)

    def test_lists_local_videos_and_serves_thumbnail(self):
        list_request = urllib.request.Request(self.base_url + "/api/library?token=test")
        with urllib.request.urlopen(list_request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["items"][0]["name"], "Episode 1.mp4")
        self.assertTrue(payload["items"][0]["has_thumbnail"])

        query = urllib.parse.urlencode({"name": "Episode 1.mp4", "token": "test", "inline": "1"})
        with urllib.request.urlopen(self.base_url + "/api/library/thumbnail?" + query) as response:
            self.assertEqual(response.read(), b"thumbnail-data")
            self.assertTrue(response.headers["Content-Disposition"].startswith("inline;"))

    def test_downloads_selected_videos_as_zip(self):
        request = urllib.request.Request(
            self.base_url + "/api/library/download?token=test",
            data=json.dumps({"names": ["Episode 1.mp4"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            archive_bytes = response.read()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.namelist(), ["Episode 1.mp4"])
            self.assertEqual(archive.read("Episode 1.mp4"), b"video-data")

    def test_rejects_unauthenticated_library_access(self):
        MediaLibraryTestHandler.authenticated = False
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base_url + "/api/library?token=invalid")
            self.assertEqual(error.exception.code, 401)
        finally:
            MediaLibraryTestHandler.authenticated = True


if __name__ == "__main__":
    unittest.main()
