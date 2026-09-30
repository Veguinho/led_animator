"""Color a video frame with the live audio palette while retaining its image."""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from audio_energy import AdaptiveAudioEnergy
from audio_palette_controls import PaletteControls, default_settings, make_adaptive_palette, make_palette
from video_controls import VideoPlaybackControls


class LiveAudioVideoTexture:
    def __init__(
        self, controls: PaletteControls, video_controls: VideoPlaybackControls,
        latest_samples: Callable[[int], np.ndarray], *, size: int = 48,
        sensitivity: float = 1.5,
    ) -> None:
        self.controls = controls
        self.video_controls = video_controls
        self.latest_samples = latest_samples
        self.size = size
        self.sensitivity = sensitivity
        self.energy = AdaptiveAudioEnergy()
        self.settings = default_settings() | {"preset": "adaptive", "brightness": 1.0}
        self.revision = -1
        self.phase = 0.0
        self.last_time: float | None = None
        self.palette = np.zeros((size, 3), dtype=np.uint8)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        now = time.monotonic()
        elapsed = 0.0 if self.last_time is None else max(0.0, now - self.last_time)
        self.last_time = now
        return self.render(frame, self.latest_samples(self.energy.fft_size), elapsed)

    def render(self, frame: np.ndarray, samples: np.ndarray, elapsed: float) -> np.ndarray:
        if frame.shape != (self.size, self.size, 3):
            raise ValueError("video texture requires a native-size RGB frame")
        revision, settings = self.controls.settings_snapshot()
        if revision != self.revision:
            if settings["preset"] != self.settings["preset"]:
                self.energy.reset()
                self.phase = 0.0
            self.revision, self.settings = revision, settings
        energy, colorful = self.energy.update(
            samples, elapsed, sensitivity=self.sensitivity,
            slowdown=self.settings["slowdown"],
        )
        palette_settings = self.settings | {"brightness": 1.0, "saturation": 1.0}
        if self.settings["preset"] == "adaptive":
            self.palette = make_adaptive_palette(
                palette_settings, energy, colorful, size=self.size,
            )
        else:
            if self.settings["preset"] == "moving-rainbow":
                self.phase = (self.phase + min(elapsed, 0.1) *
                              (1.0 - self.settings["slowdown"] / 100.0) / 8.0) % 1.0
            self.palette = make_palette(palette_settings, size=self.size, phase=self.phase)

        source = frame.astype(np.float32)
        peak = source.max(axis=2, keepdims=True) / 255.0
        tinted = peak * self.palette[None, :, :].astype(np.float32)
        tint_amount = 0.25 + 0.40 * energy
        colored = source * (1.0 - tint_amount) + tinted * tint_amount
        luma = np.sum(colored * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32),
                      axis=2, keepdims=True)
        saturation = self.settings["saturation"] * (0.35 + 0.65 * energy)
        # Video intensity is gamma-mapped later. Invert that curve here so a
        # saved audio brightness value does not darken the video twice.
        gain = self.settings["brightness"] ** (1.0 / 2.2) * (1.0 + 0.30 * energy)
        result = luma + (colored - luma) * saturation
        result = np.rint(result * gain).clip(0, 255).astype(np.uint8)
        audible = bool(
            np.sqrt(np.mean(np.square(samples, dtype=np.float64))) * self.sensitivity
            >= 0.0005
        )
        self.video_controls.set_audio_reactivity(energy, saturation, audible)
        return result

    def publish_frame(self, frame: np.ndarray) -> None:
        self.controls.publish_frame(frame, self.palette)
