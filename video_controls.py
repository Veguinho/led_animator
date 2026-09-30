"""Thread-safe state for seeking the live LED video player."""

from __future__ import annotations

import math
import threading


class VideoPlaybackControls:
    def __init__(self, name: str, duration: float, fps: float, start: float = 0.0,
                 *, connected: bool = True) -> None:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("video duration must be positive")
        self.name = name
        self.duration = duration
        self.fps = fps
        self.connected = connected
        self._lock = threading.Lock()
        self._position = self.validate_position(start)
        self._pending_seek: float | None = None
        self._playing = connected

    def validate_position(self, seconds: object) -> float:
        if type(seconds) not in (int, float) or not math.isfinite(seconds):
            raise ValueError("video time must be a finite number of seconds")
        if not 0 <= seconds < self.duration:
            raise ValueError("video time must be within the video duration")
        return float(seconds)

    def seek(self, seconds: object) -> dict:
        position = self.validate_position(seconds)
        with self._lock:
            self._pending_seek = position if self.connected else None
            self._position = position
            self._playing = self.connected
            return self._state_locked()

    def take_seek(self) -> float | None:
        with self._lock:
            seconds, self._pending_seek = self._pending_seek, None
            return seconds

    def set_position(self, seconds: float) -> None:
        with self._lock:
            if self._pending_seek is None:
                self._position = min(max(seconds, 0.0), self.duration)

    def finish(self) -> None:
        with self._lock:
            self._playing = False
            self._position = self.duration

    def _state_locked(self) -> dict:
        return {
            "name": self.name, "duration": self.duration, "fps": self.fps,
            "position": self._position, "playing": self._playing,
            "connected": self.connected,
            "seeking": self._pending_seek is not None,
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()
