"""projectM preset controls and an isolated, request/response OpenGL renderer."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import random
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".build/projectm"
BINARY = BUILD / "build/led-projectm"
PRESETS = BUILD / "vendor/cream-of-the-crop"
TEXTURES = BUILD / "vendor/presets/textures"
FAVORITES_PATH = ROOT / ".build/projectm-favorites.json"
SETUP = "Run .venv/bin/python scripts/setup_projectm.py to build projectM and install presets."


class ProjectMRenderer:
    """Own graphics on the helper's main thread; never on the HTTP thread."""

    def __init__(self, presets: list[Path], size: int, fps: int):
        self.size = size
        self._messages = deque(maxlen=8)
        self._temporary = tempfile.TemporaryDirectory(prefix="led-projectm-")
        self._process = None
        try:
            listing = Path(self._temporary.name) / "presets.txt"
            listing.write_text("".join(f"{path}\n" for path in presets))
            self._process = subprocess.Popen(
                [str(BINARY), str(size), str(fps), str(listing), str(TEXTURES)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            ready = threading.Event()

            def messages():
                for raw in self._process.stderr:
                    line = raw.decode(errors="replace").strip()
                    if line == "READY":
                        ready.set()
                    else:
                        self._messages.append(line)
                ready.set()

            self._reader = threading.Thread(target=messages, daemon=True, name="projectm-log")
            self._reader.start()
            if not ready.wait(15) or self._process.poll() is not None:
                raise RuntimeError("projectM startup failed: " + self.message())
        except Exception:
            self.close()
            raise

    def message(self):
        return "; ".join(self._messages) or "renderer stopped or timed out"

    def render(self, samples: np.ndarray, index: int, revision: int) -> np.ndarray:
        pcm = np.nan_to_num(np.asarray(samples, dtype="<f4").reshape(-1)[-48000:]).clip(-1, 1)
        packet = struct.pack("<III", len(pcm), index, revision) + pcm.tobytes()
        deadline = time.monotonic() + 5
        # Nonblocking writes and reads bound even a stuck GPU/helper.
        source, target = self._process.stdin.fileno(), self._process.stdout.fileno()
        os.set_blocking(source, False)
        pending = memoryview(packet)
        while pending:
            if not select.select([], [source], [], max(0, deadline - time.monotonic()))[1]:
                raise RuntimeError("projectM audio pipe timed out")
            try:
                pending = pending[os.write(source, pending):]
            except BlockingIOError:
                continue
        remaining = self.size * self.size * 3
        chunks = []
        while remaining:
            if not select.select([target], [], [], max(0, deadline - time.monotonic()))[0]:
                raise RuntimeError("projectM render timed out")
            chunk = os.read(target, remaining)
            if not chunk:
                raise RuntimeError(self.message())
            remaining -= len(chunk)
            chunks.append(chunk)
        if self._messages and self._messages[-1].startswith("PRESET_ERROR"):
            raise RuntimeError(self.message())
        return np.frombuffer(b"".join(chunks), dtype=np.uint8).reshape(self.size, self.size, 3)

    def close(self):
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            if hasattr(self, "_reader"):
                self._reader.join(timeout=1)
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                pipe.close()
            self._process = None
        self._temporary.cleanup()


class ProjectMControls:
    def __init__(self, size: int):
        self.size = size
        self._lock = threading.Lock()
        self._presets = sorted(PRESETS.rglob("*.milk")) if PRESETS.is_dir() else []
        self._preset_keys = [path.relative_to(PRESETS).as_posix() for path in self._presets]
        self._favorites = set()
        try:
            saved = json.loads(FAVORITES_PATH.read_text())
            if not isinstance(saved, list) or any(not isinstance(key, str) for key in saved):
                raise ValueError("expected a list of preset paths")
            self._favorites = set(saved)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            print(f"Could not restore projectM favorites: {exc}", file=sys.stderr)
        self._index = 0
        # A bright, broad-field starter reads better on the small panel.
        for index, path in enumerate(self._presets):
            if "Geiss - Cosmic Dust 2" in path.stem:
                self._index = index
                break
        self._active = False
        self._auto = True
        self._duration = 30
        self._revision = 0
        self._changed = 0.0
        self._error = None
        self._renderer = None

    def available(self):
        return BINARY.is_file() and bool(self._presets)

    def library(self):
        return [{"id": i, "name": str(path.relative_to(PRESETS).with_suffix(""))}
                for i, path in enumerate(self._presets)]

    def state(self):
        with self._lock:
            return {"available": self.available(), "active": self._active,
                    "preset": self._index, "name": self._presets[self._index].stem if self._presets else "",
                    "count": len(self._presets), "auto": self._auto, "duration": self._duration,
                    "favorites": [i for i, key in enumerate(self._preset_keys) if key in self._favorites],
                    "error": self._error, "size": self.size, "render_size": self.size * 4}

    def action(self, action: str, payload: object):
        if action in ("start", "stop", "next", "previous", "randomize"):
            if payload != {}:
                raise ValueError("this action expects an empty JSON object")
        elif action in ("preset", "favorite-play"):
            if (not isinstance(payload, dict) or payload.keys() != {"preset"}
                    or type(payload["preset"]) is not int or not 0 <= payload["preset"] < len(self._presets)):
                raise ValueError("choose a projectM preset from the list")
        elif action == "favorite":
            if (not isinstance(payload, dict) or payload.keys() != {"preset", "favorite"}
                    or type(payload["preset"]) is not int or not 0 <= payload["preset"] < len(self._presets)
                    or type(payload["favorite"]) is not bool):
                raise ValueError("choose a projectM preset and a boolean favorite value")
        elif action == "settings":
            if (not isinstance(payload, dict) or payload.keys() != {"auto", "duration"}
                    or type(payload["auto"]) is not bool or type(payload["duration"]) is not int
                    or not 10 <= payload["duration"] <= 300):
                raise ValueError("auto must be boolean and duration an integer from 10 to 300 seconds")
        else:
            raise ValueError("unknown projectM action")
        with self._lock:
            if action == "stop":
                self._active = False
            elif action == "favorite":
                favorites = self._favorites.copy()
                key = self._preset_keys[payload["preset"]]
                if payload["favorite"]:
                    favorites.add(key)
                else:
                    favorites.discard(key)
                FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
                temporary = FAVORITES_PATH.with_suffix(".tmp")
                temporary.write_text(json.dumps(sorted(favorites)) + "\n")
                temporary.replace(FAVORITES_PATH)
                self._favorites = favorites
            elif action == "settings":
                self._auto, self._duration = payload["auto"], payload["duration"]
                self._changed = time.monotonic()
            else:
                if not self.available():
                    raise ValueError(SETUP)
                if action == "start":
                    self._active, self._error = True, None
                elif action == "randomize":
                    self._index = random.randrange(len(self._presets))
                    self._active, self._error, self._auto = True, None, False
                elif action == "favorite-play":
                    if self._preset_keys[payload["preset"]] not in self._favorites:
                        raise ValueError("choose a preset from your favorites")
                    self._index = payload["preset"]
                    self._active, self._error, self._auto = True, None, False
                elif action == "preset":
                    self._index = payload["preset"]
                else:
                    self._index = (self._index + (1 if action == "next" else -1)) % len(self._presets)
                self._revision += 1
                self._changed = time.monotonic()

    def render(self, samples, brightness, saturation, fps=12):
        with self._lock:
            active = self._active
            if active and self._auto and time.monotonic() - self._changed >= self._duration:
                self._index = (self._index + random.randrange(1, max(2, len(self._presets)))) % len(self._presets)
                self._revision += 1
                self._changed = time.monotonic()
            index, revision = self._index, self._revision
        if not active:
            self.close()
            return None
        try:
            if self._renderer is None:
                self._renderer = ProjectMRenderer(self._presets, self.size, max(1, round(fps)))
            frame = self._renderer.render(samples, index, revision).astype(np.float32)
            # Keep projectM's own colors; shared saturation and brightness are
            # applied once before RGB565 encoding and the board's output cap.
            gray = frame @ np.array([.2126, .7152, .0722], dtype=np.float32)
            return np.rint((gray[..., None] + (frame - gray[..., None]) * saturation)
                           * brightness).clip(0, 255).astype(np.uint8)
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._active, self._error = False, str(exc)
            self.close()
            return None

    def close(self):
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
