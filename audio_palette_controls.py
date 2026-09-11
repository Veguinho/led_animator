"""Live palette state and a loopback-only, dependency-free control panel."""

from __future__ import annotations

import colorsys
import copy
import json
import re
import threading
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np


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
        "preset": "adaptive", "colors": PRESETS["adaptive"].copy(),
        "blend": "gradient", "brightness": 0.6, "saturation": 1.0,
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

    def __init__(self, slowdown: float = DEFAULT_SLOWDOWN) -> None:
        self._lock = threading.Lock()
        self._settings = validate_settings(default_settings() | {"slowdown": slowdown})
        self._palette = make_palette()
        self._display_palette = self._palette.copy()
        self._revision = 0
        self._frame = np.zeros((16, 16, 3), dtype=np.uint8)

    def update(self, settings: object) -> None:
        validated = validate_settings(settings)
        palette = make_palette(validated)
        with self._lock:
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
            if palette is not None:
                self._display_palette = palette.copy()

    def state(self) -> dict:
        with self._lock:
            return {
                "settings": copy.deepcopy(self._settings),
                "revision": self._revision,
                "palette": self._display_palette.tolist(), "frame": self._frame.tolist(),
                "presets": copy.deepcopy(PRESETS),
            }


class PaletteServer:
    def __init__(
        self, controls: PaletteControls, port: int = 8765,
        on_refresh: Callable[[], None] | None = None,
    ) -> None:
        page = Path(__file__).with_name("audio_palette_controls.html").read_bytes()
        session = uuid.uuid4().hex

        def state() -> dict:
            return controls.state() | {"session": session, "refresh_available": on_refresh is not None}

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
                if self.path == "/":
                    self.respond(200, page, "text/html; charset=utf-8")
                elif self.path == "/api/state":
                    self.json_response(200, state())
                else:
                    self.json_response(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path not in ("/api/palette", "/api/refresh"):
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
                    else:
                        controls.update(payload)
                except (ValueError, UnicodeError) as exc:
                    self.json_response(400, {"error": str(exc)})
                    return
                if self.path == "/api/refresh":
                    if on_refresh is None:
                        self.json_response(503, {"error": "refresh is unavailable in this preview"})
                        return
                    self.json_response(202, {"restarting": True, "session": session})
                    self.wfile.flush()
                    on_refresh()
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
