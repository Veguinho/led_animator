"""Minimal loopback-only Play/Pause controls for MP4 streaming."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PlaybackControls:
    def __init__(self) -> None:
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._connection = "connecting"

    def pause(self) -> None:
        self._paused.set()

    def play(self) -> None:
        self._paused.clear()

    def paused(self) -> bool:
        return self._paused.is_set()

    def set_connection(self, status: str) -> None:
        if status not in ("connecting", "live", "reconnecting"):
            raise ValueError("invalid playback connection state")
        with self._lock:
            self._connection = status

    def state(self) -> dict:
        with self._lock:
            connection = self._connection
        return {"playing": not self.paused(), "connection": connection}


class PlaybackControlServer:
    def __init__(self, controls: PlaybackControls, port: int = 8765) -> None:
        page = Path(__file__).with_name("playback_controls.html").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def respond(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def json_response(self, status: int, value: dict) -> None:
                self.respond(status, json.dumps(value).encode(), "application/json")

            def do_GET(self) -> None:
                if self.path in ("/", "/video"):
                    self.respond(200, page, "text/html; charset=utf-8")
                elif self.path == "/api/video/state":
                    self.json_response(200, controls.state())
                else:
                    self.json_response(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path not in ("/api/video/play", "/api/video/pause"):
                    self.json_response(404, {"error": "not found"})
                    return
                allowed_host = f"127.0.0.1:{self.server.server_port}"
                if (self.headers.get("Host") != allowed_host or
                    self.headers.get("Origin", f"http://{allowed_host}") != f"http://{allowed_host}"):
                    self.json_response(403, {"error": "use the local control panel URL"})
                    return
                if self.path.endswith("/play"):
                    controls.play()
                else:
                    controls.pause()
                self.json_response(200, controls.state())

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(2)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}/video"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="video-playback-controls", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=2)
        self._server.server_close()
