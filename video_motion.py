"""Adaptive video tint and playback speed driven by full-spectrum music energy."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from functools import lru_cache

import numpy as np

from audio_energy import AdaptiveAudioEnergy
from audio_palette_controls import PaletteControls
from video_audio_texture import make_video_energy_palette
from video_controls import VideoPlaybackControls


def _luminance(rgb: np.ndarray) -> np.ndarray:
    # Small LED grids need only three channel operations, not a BLAS call.
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def speed_for_energy(energy: float) -> float:
    # Map the full energy range from 20% slower to 150% playback speed.
    return 0.8 + 0.7 * float(np.clip(energy, 0.0, 1.0))


def spectral_fullness(amplitudes: np.ndarray) -> float:
    """Require bass, mids and highs together for the strongest response."""
    strongest = float(np.max(amplitudes))
    if strongest <= 0:
        return 0.0
    relative_db = 20.0 * np.log10(max(float(np.min(amplitudes)) / strongest, 1e-12))
    # Allow the natural bass-heavy balance of music. A missing band or FFT
    # leakage from an isolated tone must not count as a full arrangement.
    presence = float(np.clip((relative_db + 36.0) / 18.0, 0.0, 1.0))
    return presence * presence * (3.0 - 2.0 * presence)


@lru_cache(maxsize=4)
def _wave_coordinates(size: int) -> tuple[np.ndarray, ...]:
    y, x = np.indices((size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    dx, dy = (x - center) / max(center, 1.0), (y - center) / max(center, 1.0)
    radius = np.hypot(dx, dy)
    angle = np.arctan2(dy, dx)
    # Fade displacement at all four edges to keep the image inside its frame.
    edge = np.maximum(0.0, (1.0 - dx * dx) * (1.0 - dy * dy))
    return x, y, dx, dy, radius, angle, edge


def bend_video_texture(frame: np.ndarray, energy: float,
                       phase: float) -> tuple[np.ndarray, np.ndarray | None]:
    """Gently curve the video and carry color waves along its own contours."""
    strength = float(np.clip((energy - 0.82) / 0.18, 0.0, 1.0))
    strength = strength * strength * (3.0 - 2.0 * strength)
    if strength == 0.0:
        return frame, None
    size = frame.shape[0]
    x, y, dx, dy, radius, angle, edge = _wave_coordinates(size)
    source = frame.astype(np.float32)
    detail = (0.65 * source.max(axis=2)
              + 0.35 * _luminance(source)) / 255.0
    # Luminance bends the circular phase around the shapes in the current
    # frame, rather than laying independent rings over the video.
    contour = radius * 7.0 + detail * 4.0 - phase
    wave = np.sin(contour + 0.65 * np.sin(2.0 * angle - phase))
    curl = np.sin(contour * 0.65 + 2.0 * angle - phase)
    amount = size * 0.035 * strength * edge
    sample_x = np.clip(x + amount * (0.65 * wave * dx - 0.75 * curl * dy), 0, size - 1)
    sample_y = np.clip(y + amount * (0.65 * wave * dy + 0.75 * curl * dx), 0, size - 1)
    left, top = np.floor(sample_x).astype(np.intp), np.floor(sample_y).astype(np.intp)
    right, bottom = np.minimum(left + 1, size - 1), np.minimum(top + 1, size - 1)
    mix_x, mix_y = (sample_x - left)[..., None], (sample_y - top)[..., None]
    bent = ((source[top, left] * (1.0 - mix_x) + source[top, right] * mix_x) * (1.0 - mix_y)
            + (source[bottom, left] * (1.0 - mix_x) + source[bottom, right] * mix_x) * mix_y)
    # Offset the lookup through the bright palette; intensity still masks it
    # to the video texture, including its black regions.
    return bent, strength * (0.20 * wave + 0.08 * curl)


def tint_video(frame: np.ndarray, palette: np.ndarray, *,
               saturation: float, brightness: float, gamma: float = 2.2,
               palette_shift: np.ndarray | None = None) -> np.ndarray:
    """Color the video's own light and shadow, keeping black pixels black."""
    source = frame.astype(np.float32)
    luma = _luminance(source)
    # Retain detail in saturated footage as well as neutral highlights.
    peak_channel = np.maximum(np.maximum(source[..., 0], source[..., 1]), source[..., 2])
    intensity = (0.65 * peak_channel + 0.35 * luma) / 255.0
    positions = intensity
    if palette_shift is not None:
        positions = np.clip(positions + palette_shift, 0.0, 1.0)
    positions = positions * (len(palette) - 1)
    tinted = np.stack([
        np.interp(positions, np.arange(len(palette)), palette[:, channel])
        for channel in range(3)
    ], axis=-1) * intensity[..., None]
    neutral = _luminance(tinted)
    tinted = neutral[..., None] + (tinted - neutral[..., None]) * saturation
    # Work in the final LED intensity range, then invert the stream's intensity
    # curve. Otherwise dim blue-purple falls below the first RGB565 step.
    tinted *= 0.25 * brightness
    peak = np.maximum(np.maximum(tinted[..., 0], tinted[..., 1]), tinted[..., 2])[..., None] / 255.0
    gain = np.zeros_like(peak)
    np.power(peak, 1.0 / gamma - 1.0, out=gain, where=peak > 0)
    tinted *= gain
    return np.rint(tinted).clip(0, 255).astype(np.uint8)


class LiveAudioVideoMotion:
    """Drive video speed, temperature, saturation and light with one envelope."""

    def __init__(self, controls: PaletteControls, video_controls: VideoPlaybackControls,
                 latest_samples: Callable[[int], np.ndarray], *, size: int = 48,
                 gamma: float = 2.2) -> None:
        self.controls = controls
        self.video_controls = video_controls
        self.latest_samples = latest_samples
        self.size = size
        self.gamma = gamma
        self.energy = AdaptiveAudioEnergy()
        self.fullness = 0.0
        self.wave_phase = 0.0
        self.last_time: float | None = None
        self.palette = np.zeros((size, 3), dtype=np.uint8)
        video_controls.set_layers({"audio_palette": False})

    def apply(self, frame: np.ndarray, *,
              select_frame: Callable[[np.ndarray], np.ndarray] | None = None) -> np.ndarray:
        now = time.monotonic()
        elapsed = 1.0 / self.video_controls.fps if self.last_time is None else now - self.last_time
        self.last_time = now
        return self.render(frame, self.latest_samples(4096), elapsed, select_frame=select_frame)

    def render(self, frame: np.ndarray, samples: np.ndarray, elapsed: float, *,
               select_frame: Callable[[np.ndarray], np.ndarray] | None = None) -> np.ndarray:
        _, settings = self.controls.settings_snapshot()
        energy, _colorful = self.energy.update(samples, elapsed, slowdown=settings["slowdown"])
        fullness_target = spectral_fullness(self.energy.band_amplitudes)
        dt = min(max(elapsed, 0.0), 0.1) / (1.0 + 3.0 * settings["slowdown"] / 95.0)
        tau = 0.8 if fullness_target > self.fullness else 1.4
        self.fullness += (fullness_target - self.fullness) * (-np.expm1(-dt / tau))
        # Loud but sparse passages still move; the final 55% of drive requires
        # simultaneous bass, midrange and treble, not just a single loud band.
        energy *= 0.45 + 0.55 * self.fullness
        # Silence returns immediately to the calm look and 0.8x playback;
        # audible passages use the smoothed Adaptive and fullness envelopes.
        tail = samples[-self.energy.fft_size:]
        audible = bool(len(tail) and np.sqrt(np.mean(np.square(tail, dtype=np.float64)))
                       * 1.5 >= 0.0005)
        if not audible:
            energy = self.fullness = 0.0
        saturation = settings["saturation"] * (0.55 + 0.45 * energy)
        self.video_controls.set_playback_speed(speed_for_energy(energy))
        self.video_controls.set_content_fps(self.video_controls.fps)
        self.video_controls.set_audio_reactivity(energy, saturation, audible)
        self.palette = make_video_energy_palette(energy, size=self.size,
                                                reverse=settings["reverse"])
        frame = frame if select_frame is None else select_frame(frame)
        if not self.video_controls.output_layers()["video"]:
            return np.zeros_like(frame)
        # Use the same smoothed full-spectrum energy as tint and speed. The
        # peak wave grows continuously and Slowdown also slows its rotation.
        self.wave_phase = (self.wave_phase + dt * energy * 2.0 * np.pi * 0.24) % (2.0 * np.pi)
        frame, palette_shift = bend_video_texture(frame, energy, self.wave_phase)
        return tint_video(frame, self.palette, saturation=saturation,
                          brightness=settings["brightness"], gamma=self.gamma,
                          palette_shift=palette_shift)

    def publish_frame(self, frame: np.ndarray) -> None:
        self.controls.publish_frame(frame, self.palette)


class MotionVideoTimeline:
    """Consume source frames by video time while panel ticks stay evenly spaced.

    Decode only the next needed frames, at most 1.5 native seconds per wall
    second. Memory stays constant even for the long liquid background clip.
    """

    def __init__(self, source_fps: float, controls: VideoPlaybackControls,
                 decode_at: Callable[[float], Iterator[np.ndarray]]) -> None:
        self.source_fps = source_fps
        self.controls = controls
        self.decode_at = decode_at
        self.decoder: Iterator[np.ndarray] | None = None
        self.frame: np.ndarray | None = None
        self.index = 0
        self.offset = 0.0
        self.start = 0.0
        self.first = True
        self.exhausted = False

    def frames_at(self, seconds: float = 0.0) -> Iterator[bytes]:
        self.start, self.offset, self.index = seconds, 0.0, 0
        self.first, self.exhausted = True, False
        self.decoder = iter(self.decode_at(seconds))
        try:
            self.frame = next(self.decoder, None)
            if self.frame is None:
                return
            while not self.exhausted:
                # Audio and frame selection happen later, at the send deadline.
                yield self.frame.tobytes()
        finally:
            close = getattr(self.decoder, "close", None)
            if close is not None:
                close()
            self.decoder = None

    def select(self, frame: np.ndarray) -> np.ndarray:
        assert self.frame is not None and self.decoder is not None
        if self.controls.is_paused():
            return self.frame
        if self.first:
            self.first = False
        else:
            self.offset += self.controls.playback_speed() / self.controls.fps
        target = int(self.offset * self.source_fps + 1e-9)
        while self.index < target:
            decoded = next(self.decoder, None)
            if decoded is None:
                self.exhausted = True
                break
            self.frame = decoded
            self.index += 1
        self.controls.set_position(self.start + self.index / self.source_fps)
        return self.frame
