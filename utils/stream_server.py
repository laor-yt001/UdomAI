import os
import threading
import http.server
import socketserver
import time
from pathlib import Path

class StreamServer:
    _instance = None
    _lock = threading.Lock()

    def __init__(self, root_dir: str, port: int = 8000, ttl_seconds: int = 3600):
        self.root_dir = Path(root_dir).resolve()
        self.port = port
        self.ttl_seconds = ttl_seconds
        self._start_server()
        self._start_cleanup()

    @classmethod
    def get_instance(cls, root_dir: str = None, port: int = 8000, ttl_seconds: int = 3600):
        with cls._lock:
            if cls._instance is None:
                if root_dir is None:
                    raise ValueError("root_dir must be provided for first StreamServer initialization")
                cls._instance = cls(root_dir, port, ttl_seconds)
            return cls._instance

    def _start_server(self):
        os.chdir(self.root_dir)
        handler = http.server.SimpleHTTPRequestHandler
        self.httpd = socketserver.TCPServer(("0.0.0.0", self.port), handler)
        self._server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._server_thread.start()
        print(f"[StreamServer] Serving {self.root_dir} at http://0.0.0.0:{self.port}")

    def _start_cleanup(self):
        def cleanup_loop():
            while True:
                now = time.time()
                for child in self.root_dir.iterdir():
                    if child.is_dir():
                        try:
                            mod_time = child.stat().st_mtime
                            if now - mod_time > self.ttl_seconds:
                                for f in child.rglob('*'):
                                    if f.is_file():
                                        f.unlink()
                                child.rmdir()
                        except Exception:
                            pass
                time.sleep(self.ttl_seconds // 2)
        self._cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def get_url(self, manifest_path: str) -> str:
        rel = Path(manifest_path).resolve().relative_to(self.root_dir)
        return f"http://127.0.0.1:{self.port}/{rel.as_posix()}"
