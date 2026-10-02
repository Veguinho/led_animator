"""Live palette state and a loopback-only, dependency-free control panel."""

from __future__ import annotations

import colorsys
import base64
import copy
import json
import re
import sys
import threading
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from video_controls import VideoPlaybackControls


PRESETS = {
    "rainbow": ["#ff0000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff"],
    "moving-rainbow": ["#ff0000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff"],
    "adaptive": ["#ff3300", "#ffbb00", "#93dfff", "#859cff"],
    "blue-red": ["#0d1eb8", "#b800ff", "#ff0000"],
    "sunset": ["#ffbe0b", "#ff5400", "#ff006e", "#8338ec"],
    "ocean": ["#052c80", "#0077ff", "#00e5ff", "#80ffdb"],
    "neon": ["#ff00aa", "#8000ff", "#00ffff"],
    "fire": ["#ff1800", "#ff8000", "#ffe066"],
}

DEFAULT_SLOWDOWN = 20.0


def default_settings() -> dict:
    return {
        "preset": "custom", "colors": ["#ff0000"],
        "blend": "gradient", "brightness": 16 / 255, "saturation": 1.0,
        "reverse": False, "slowdown": DEFAULT_SLOWDOWN,
    }


def validate_settings(settings: object) -> dict:
    if not isinstance(settings, dict) or settings.keys() != default_settings().keys():
        raise ValueError("send a complete palette configuration")
    result = copy.deepcopy(settings)
    if not isinstance(result["preset"], str) or result["preset"] not in (*PRESETS, "custom"):
        raise ValueError("unknown palette preset")
    colors = result["colors"]
    if not isinstance(colors, list) or not 1 <= len(colors) <= 6 or not all(
        isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        for color in colors
    ):
        raise ValueError("choose one to six colors in #RRGGBB format")
    if result["blend"] not in ("gradient", "bands"):
        raise ValueError("blend must be gradient or bands")
    for key in ("brightness", "saturation"):
        value = result[key]
        if type(value) not in (int, float) or not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    if type(result["reverse"]) is not bool:
        raise ValueError("reverse must be true or false")
    validate_slowdown(result["slowdown"])
    if result["preset"] != "custom":
        result["colors"] = PRESETS[result["preset"]].copy()
        if result["preset"] in ("moving-rainbow", "adaptive"):
            result["blend"] = "gradient"
    else:
        result["colors"] = [color.lower() for color in colors]
    return result


def validate_slowdown(value: object) -> None:
    if type(value) not in (int, float) or not np.isfinite(value) or not 0 <= value <= 95:
        raise ValueError("slowdown must be between 0 and 95 percent")


def make_palette(settings: dict | None = None, size: int = 16, phase: float = 0.0) -> np.ndarray:
    settings = validate_settings(default_settings() if settings is None else settings)
    if settings["preset"] == "adaptive":
        return make_adaptive_palette(settings, size=size)
    if settings["preset"] == "moving-rainbow":
        # A periodic hue wheel avoids a discontinuity between the panel edges.
        # Fractional phase shifts colors smoothly, instead of rolling columns.
        hues = (np.arange(size) / size - phase) % 1.0
        palette = np.rint(np.array([
            colorsys.hsv_to_rgb(float(hue), settings["saturation"], settings["brightness"])
            for hue in hues
        ]) * 255).clip(0, 255).astype(np.uint8)
        return palette[::-1].copy() if settings["reverse"] else palette
    stops = np.array([
        [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        for color in settings["colors"]
    ])
    if settings["blend"] == "bands":
        colors = stops[np.minimum(np.arange(size) * len(stops) // size, len(stops) - 1)]
    else:
        positions = np.linspace(0, len(stops) - 1, size)
        colors = np.column_stack([
            np.interp(positions, np.arange(len(stops)), stops[:, channel])
            for channel in range(3)
        ])
    adjusted = []
    for color in colors:
        hue, saturation, value = colorsys.rgb_to_hsv(*color)
        adjusted.append(colorsys.hsv_to_rgb(
            hue, saturation * settings["saturation"], value * settings["brightness"]
        ))
    palette = np.rint(np.array(adjusted) * 255).clip(0, 255).astype(np.uint8)
    return palette[::-1].copy() if settings["reverse"] else palette


def make_adaptive_palette(
    settings: dict, energy: float = 0.0, colorful: float = 0.0, size: int = 16,
) -> np.ndarray:
    """Blend a dimmer, softer Ocean palette into vivid warm colors."""
    positions = np.linspace(0, 1, size)
    ocean = make_palette(settings | {
        "preset": "ocean", "brightness": 1, "saturation": 1,
        "reverse": False, "blend": "gradient",
    }, size=size)
    ocean_hsv = np.array([colorsys.rgb_to_hsv(*(color / 255.0)) for color in ocean])
    cool = ocean_hsv[:, 0] - 1.0
    warm = np.linspace(-0.04, 0.14, size)
    # Most of the energetic palette is pink, red, orange, and gold, with
    # violet/cyan accents for variety. Unwrapped hues cross red smoothly.
    vivid = np.interp(positions, [0, .2, .4, .6, .8, 1], [-.12, 0, .14, .04, -.12, -.45])
    hot = warm * (1.0 - colorful) + vivid * colorful
    hues = cool * (1.0 - energy) + hot * energy
    saturation = ocean_hsv[:, 1] * 0.6 * (1.0 - energy) + energy
    value = (ocean_hsv[:, 2] * 0.28 * (1.0 - energy) + energy) * settings["brightness"]
    palette = np.rint(np.array([
        colorsys.hsv_to_rgb(float(hue % 1.0), sat * settings["saturation"], val)
        for hue, sat, val in zip(hues, saturation, value)
    ]) * 255).clip(0, 255).astype(np.uint8)
    return palette[::-1].copy() if settings["reverse"] else palette


class PaletteControls:
    """Publish whole palettes atomically; rendering owns its animation state."""

    def __init__(self, slowdown: float = DEFAULT_SLOWDOWN, *, size: int = 16,
                 settings_path: Path | None = None) -> None:
        self.size = size
        self._lock = threading.Lock()
        self._settings = validate_settings(default_settings() | {"slowdown": slowdown})
        self._settings_path = settings_path
        if settings_path is not None and settings_path.exists():
            try:
                self._settings = validate_settings(json.loads(settings_path.read_text()))
            except (OSError, ValueError) as exc:
                print(f"Could not restore palette; using defaults: {exc}", file=sys.stderr)
        self._palette = make_palette(self._settings, size=self.size)
        self._display_palette = self._palette.copy()
        self._revision = 0
        self._frame = np.zeros((self.size, self.size, 3), dtype=np.uint8)
        self._frame_revision = 0
        self._frame_rgb: str | None = None

    def update(self, settings: object) -> None:
        validated = validate_settings(settings)
        palette = make_palette(validated, size=self.size)
        with self._lock:
            if self._settings_path is not None:
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._settings_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(validated) + "\n")
                temporary.replace(self._settings_path)
            self._settings = validated
            self._palette = palette
            self._display_palette = palette.copy()
            self._revision += 1

    def palette_snapshot(self) -> tuple[int, np.ndarray, float]:
        with self._lock:
            return self._revision, self._palette.copy(), self._settings["slowdown"]

    def settings_snapshot(self) -> tuple[int, dict]:
        with self._lock:
            return self._revision, copy.deepcopy(self._settings)

    def publish_frame(self, frame: np.ndarray, palette: np.ndarray | None = None) -> None:
        with self._lock:
            self._frame = frame.copy()
            self._frame_revision += 1
            self._frame_rgb = None
            if palette is not None:
                self._display_palette = palette.copy()

    def state(self, *, compact: bool = False) -> dict:
        with self._lock:
            result = {
                "settings": copy.deepcopy(self._settings),
                "revision": self._revision,
                "palette": self._display_palette.tolist(),
                "presets": copy.deepcopy(PRESETS),
            }
            if compact:
                # Encode once per acknowledged frame, even with several clients.
                if self._frame_rgb is None:
                    self._frame_rgb = base64.b64encode(self._frame.tobytes()).decode("ascii")
                result.update(frame_rgb=self._frame_rgb,
                              frame_size=self._frame.shape[0],
                              frame_revision=self._frame_revision)
            else:
                result["frame"] = self._frame.tolist()
            return result


class PaletteServer:
    def __init__(
        self, controls: PaletteControls, port: int = 8765,
        on_refresh: Callable[[], None] | None = None,
        video_controls: VideoPlaybackControls | None = None,
        video_path: Path | None = None,
        on_video_start: Callable[[Path], None] | None = None,
        video_library: dict[str, Path] | None = None,
    ) -> None:
        if video_path is not None and not video_path.is_file():
            raise ValueError(f"video does not exist: {video_path}")
        page = Path(__file__).with_name("audio_palette_controls.html").read_bytes()
        session = uuid.uuid4().hex
        library = dict(video_library or {})
        launch_lock = threading.Lock()
        launching = False

        def state(*, compact: bool = False) -> dict:
            return controls.state(compact=compact) | {
                "session": session, "refresh_available": on_refresh is not None,
                "mode": "video" if video_controls is not None else "audio",
                "video": video_controls.state() if video_controls is not None else None,
                "video_file_available": video_path is not None,
                "video_start_available": on_video_start is not None,
                "videos": list(library),
                "video_starting": launching,
            }

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

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

            def serve_video(self, *, head_only: bool = False) -> None:
                if video_path is None:
                    self.json_response(404, {"error": "no video file is available"})
                    return
                size = video_path.stat().st_size
                first, last = 0, size - 1
                range_header = self.headers.get("Range")
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                    if match and (match[1] or match[2]):
                        if match[1]:
                            first = int(match[1])
                            last = min(int(match[2]), size - 1) if match[2] else size - 1
                        else:
                            length = int(match[2])
                            first = max(0, size - length)
                    if not match or (not match[1] and not match[2]) or first > last or first >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                self.send_response(206 if range_header else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(last - first + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if range_header:
                    self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
                self.end_headers()
                if head_only:
                    return
                try:
                    with video_path.open("rb") as source:
                        source.seek(first)
                        remaining = last - first + 1
                        while remaining:
                            chunk = source.read(min(256 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:
                if self.path == "/":
                    self.respond(200, page, "text/html; charset=utf-8")
                elif self.path == "/api/state":
                    self.json_response(200, state())
                elif self.path == "/api/state?compact=1":
                    self.json_response(200, state(compact=True))
                elif self.path == "/api/video/file":
                    self.serve_video()
                else:
                    self.json_response(404, {"error": "not found"})

            def do_HEAD(self) -> None:
                if self.path == "/api/video/file":
                    self.serve_video(head_only=True)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self) -> None:
                nonlocal launching
                if self.path not in ("/api/palette", "/api/refresh", "/api/video/seek", "/api/video/start", "/api/video/playback", "/api/video/texture-mapping", "/api/video/layers"):
                    self.json_response(404, {"error": "not found"})
                    return
                # Accept only this local panel's JSON requests.
                allowed_host = f"127.0.0.1:{self.server.server_port}"
                if (self.headers.get("Host") != allowed_host
                    or self.headers.get("Origin", f"http://{allowed_host}") != f"http://{allowed_host}"):
                    self.json_response(403, {"error": "use the local control panel URL"})
                    return
                try:
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("expected application/json")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if self.path == "/api/refresh":
                        if payload != {}:
                            raise ValueError("refresh expects an empty JSON object")
                    elif self.path == "/api/video/start":
                        if on_video_start is None:
                            self.json_response(503, {"error": "video launch is unavailable"})
                            return
                        if (not isinstance(payload, dict) or payload.keys() != {"video"}
                            or not isinstance(payload["video"], str) or payload["video"] not in library):
                            raise ValueError("Escolha um vídeo da lista.")
                        selected_video = library[payload["video"]]
                        if not selected_video.is_file():
                            raise ValueError("O arquivo de vídeo não está mais disponível.")
                    elif self.path in ("/api/video/seek", "/api/video/playback", "/api/video/texture-mapping", "/api/video/layers"):
                        if video_controls is None:
                            self.json_response(503, {"error": "video player is not running"})
                            return
                        if self.path == "/api/video/layers":
                            video_state = video_controls.set_layers(payload)
                        elif self.path == "/api/video/texture-mapping":
                            if not isinstance(payload, dict) or payload.keys() != {"enabled"}:
                                raise ValueError("texture mapping expects an enabled boolean")
                            video_state = video_controls.set_texture_mapping(payload["enabled"])
                        elif self.path == "/api/video/playback":
                            if not isinstance(payload, dict) or payload.keys() != {"paused"}:
                                raise ValueError("playback expects a paused boolean")
                            video_state = video_controls.set_paused(payload["paused"])
                        else:
                            if not isinstance(payload, dict) or payload.keys() != {"seconds"}:
                                raise ValueError("seek expects a video time in seconds")
                            video_state = video_controls.seek(payload["seconds"])
                    else:
                        controls.update(payload)
                except (ValueError, UnicodeError) as exc:
                    self.json_response(400, {"error": str(exc)})
                    return
                except OSError:
                    self.json_response(500, {"error": "could not save palette settings"})
                    return
                if self.path == "/api/video/start":
                    with launch_lock:
                        if launching:
                            self.json_response(409, {"error": "O vídeo já está iniciando."})
                            return
                        launching = True
                    self.json_response(202, {"starting": True, "session": session})
                    self.wfile.flush()
                    on_video_start(selected_video)
                    return
                if self.path == "/api/refresh":
                    if on_refresh is None:
                        self.json_response(503, {"error": "refresh is unavailable in this preview"})
                        return
                    self.json_response(202, {"restarting": True, "session": session})
                    self.wfile.flush()
                    on_refresh()
                    return
                if self.path in ("/api/video/seek", "/api/video/playback", "/api/video/texture-mapping", "/api/video/layers"):
                    self.json_response(200, {"video": video_state})
                    return
                self.json_response(200, state())

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(2)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="palette-controls", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=2)
        self._server.server_close()
