"""Thread-safe state for seeking and pausing the live LED video player."""

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
        self._content_fps = fps
        self.connected = connected
        self._lock = threading.Lock()
        self._position = self.validate_position(start)
        self._pending_seek: float | None = None
        self._playing = connected
        self._paused = False
        self._audio_reactive = False
        self._audio_energy = 0.0
        self._audio_saturation = 0.0
        self._audio_audible = False
        self._layers = {"video": True, "audio_palette": True}

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
            self._playing = self.connected and not self._paused
            return self._state_locked()

    def set_paused(self, paused: object) -> dict:
        if type(paused) is not bool:
            raise ValueError("paused must be true or false")
        with self._lock:
            self._paused = paused
            if not paused and self._position >= self.duration:
                self._position = 0.0
                self._pending_seek = 0.0 if self.connected else None
            self._playing = self.connected and not paused
            return self._state_locked()

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_texture_mapping(self, enabled: object) -> dict:
        if type(enabled) is not bool:
            raise ValueError("texture mapping enabled must be true or false")
        return self.set_layers({"audio_palette": enabled})

    def set_layers(self, layers: object) -> dict:
        if (not isinstance(layers, dict) or not layers
            or not layers.keys() <= {"video", "audio_palette"}
            or any(type(enabled) is not bool for enabled in layers.values())):
            raise ValueError("layers must contain video or audio_palette booleans")
        with self._lock:
            self._layers.update(layers)
            if not self._layers["audio_palette"]:
                self._content_fps = self.fps
                self._audio_energy = self._audio_saturation = 0.0
                self._audio_audible = False
            return self._state_locked()

    def output_layers(self) -> dict[str, bool]:
        with self._lock:
            return self._layers.copy()

    def texture_mapping_enabled(self) -> bool:
        with self._lock:
            return self._layers["audio_palette"]

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

    def set_audio_reactivity(self, energy: float, saturation: float,
                             audible: bool) -> None:
        with self._lock:
            self._audio_reactive = True
            self._audio_energy = min(max(float(energy), 0.0), 1.0)
            self._audio_saturation = min(max(float(saturation), 0.0), 1.0)
            self._audio_audible = bool(audible)

    def set_content_fps(self, fps: float) -> None:
        with self._lock:
            self._content_fps = min(max(float(fps), 0.0), self.fps)

    def content_fps(self) -> float:
        with self._lock:
            return self._content_fps

    def _state_locked(self) -> dict:
        return {
            "name": self.name, "duration": self.duration, "fps": self.fps,
            "content_fps": self._content_fps,
            "texture_mapping_enabled": self._layers["audio_palette"],
            "layers": self._layers.copy(),
            "position": self._position, "playing": self._playing,
            "paused": self._paused,
            "connected": self.connected,
            "seeking": self._pending_seek is not None,
            "audio_reactive": {
                "enabled": self._audio_reactive,
                "audible": self._audio_audible,
                "energy": self._audio_energy,
                "saturation": self._audio_saturation,
            },
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()
