"""Latest-only Doom framebuffer mailbox, shared by HTTP and the USB worker."""

from __future__ import annotations

import base64
from collections import deque
import threading
import time
import uuid

import numpy as np


class DoomControls:
    TIMEOUT = 2.0
    FPS = 30

    def __init__(self, size: int):
        self.size = size
        self._lock = threading.Lock()
        self._token: str | None = None
        self._frame: np.ndarray | None = None
        self._updated = 0.0
        self._sent = deque(maxlen=121)

    def _active(self) -> bool:
        return self._token is not None and time.monotonic() - self._updated < self.TIMEOUT

    def start(self) -> dict:
        with self._lock:
            if self._active():
                raise ValueError("Doom is already playing in another tab. Stop it first.")
            self._token = uuid.uuid4().hex
            self._frame = None
            self._updated = time.monotonic()
            self._sent.clear()
            return {"token": self._token, "size": self.size, "fps": self.FPS}

    def update(self, payload: object) -> None:
        if (not isinstance(payload, dict) or payload.keys() != {"token", "rgb"}
                or not isinstance(payload["rgb"], str)):
            raise ValueError("Doom expects a session token and base64 RGB frame")
        try:
            raw = base64.b64decode(payload["rgb"], validate=True)
        except ValueError as exc:
            raise ValueError("invalid Doom RGB frame") from exc
        if len(raw) != self.size * self.size * 3:
            raise ValueError("Doom frame does not match the LED display size")
        with self._lock:
            if not self._active() or payload["token"] != self._token:
                raise ValueError("Doom session expired; click Play Doom again")
            self._frame = np.frombuffer(raw, dtype=np.uint8).reshape(self.size, self.size, 3).copy()
            self._updated = time.monotonic()

    def stop(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload.keys() != {"token"}:
            raise ValueError("Doom stop expects a session token")
        with self._lock:
            if payload["token"] == self._token:
                self._token, self._frame = None, None

    def frame(self, brightness: float) -> np.ndarray | None:
        with self._lock:
            if not self._active() or self._frame is None:
                return None
            return np.rint(self._frame * brightness).clip(0, 255).astype(np.uint8)

    def state(self) -> dict:
        with self._lock:
            fps = ((len(self._sent) - 1) / (self._sent[-1] - self._sent[0])
                   if len(self._sent) > 1 and self._sent[-1] > self._sent[0] else 0)
            return {"active": self._active(), "size": self.size, "fps": self.FPS,
                    "output_fps": round(fps, 1)}

    def acknowledge_frame(self) -> None:
        with self._lock:
            self._sent.append(time.monotonic())
