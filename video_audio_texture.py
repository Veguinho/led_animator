"""Render a circular audio layer and compose it with an independent video layer."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from functools import lru_cache

import numpy as np

from audio_energy import AdaptiveAudioEnergy
from audio_palette_controls import PaletteControls, default_settings, make_palette
from video_controls import VideoPlaybackControls


VIDEO_ENERGY_STOPS = np.array([
    # Quiet: violet and deep blue.
    [[44, 13, 91], [36, 15, 94], [20, 24, 87], [10, 28, 78], [27, 11, 77]],
    # Middle: red moves across the circle into green.
    [[165, 20, 47], [215, 40, 24], [187, 49, 27], [37, 164, 64], [16, 116, 51]],
    # Energetic: orange through gold and yellow.
    [[233, 82, 8], [246, 122, 9], [255, 169, 20], [255, 203, 39], [255, 228, 91]],
], dtype=np.float32)

# Keep the palette vivid on its own, but let video detail show through it.
VIDEO_OVERLAY_OPACITY = 0.45


@lru_cache(maxsize=4)
def _video_energy_stages(size: int) -> np.ndarray:
    positions = np.linspace(0, VIDEO_ENERGY_STOPS.shape[1] - 1, size)
    return np.stack([
        np.column_stack([
            np.interp(positions, np.arange(VIDEO_ENERGY_STOPS.shape[1]), stops[:, channel])
            for channel in range(3)
        ])
        for stops in VIDEO_ENERGY_STOPS
    ])


def make_video_energy_palette(energy: float, *, size: int = 48,
                              reverse: bool = False) -> np.ndarray:
    """Move smoothly from cool colors through red/green to orange/yellow."""
    stages = _video_energy_stages(size)
    level = float(np.clip(energy, 0.0, 1.0))
    if level <= 0.5:
        lower = 0
        blend = float(np.clip((level - 0.1) / 0.4, 0.0, 1.0))
    else:
        lower = 1
        blend = float(np.clip((level - 0.5) / 0.3, 0.0, 1.0))
    blend = blend * blend * (3.0 - 2.0 * blend)
    palette = np.rint(stages[lower] * (1.0 - blend) + stages[lower + 1] * blend)
    result = palette.clip(0, 255).astype(np.uint8)
    return result[::-1].copy() if reverse else result


def _spatial_energy_colors(stages: np.ndarray, energy: np.ndarray) -> np.ndarray:
    """Blend already sampled palette stages using each pixel's local energy."""
    warm = energy > 0.5
    blend = np.where(warm, (energy - 0.5) / 0.3, (energy - 0.1) / 0.4)
    blend = np.clip(blend, 0.0, 1.0)
    blend = (blend * blend * (3.0 - 2.0 * blend))[..., None]
    lower = np.where(warm[..., None], stages[1], stages[0])
    upper = np.where(warm[..., None], stages[2], stages[1])
    return lower * (1.0 - blend) + upper * blend


def compose_output_layers(video: np.ndarray, audio_palette: np.ndarray,
                          layers: dict[str, bool], *,
                          opacity: float | np.ndarray = VIDEO_OVERLAY_OPACITY) -> np.ndarray:
    """Screen-blend a translucent audio overlay; no layers means black."""
    if not layers["audio_palette"]:
        return video if layers["video"] else np.zeros_like(video)
    if not layers["video"]:
        return audio_palette
    source = video.astype(np.float32)
    alpha = np.asarray(opacity, dtype=np.float32)
    if alpha.ndim == 2:
        alpha = alpha[..., None]
    overlay = audio_palette.astype(np.float32) * alpha
    return np.rint(source + overlay * (1.0 - source / 255.0))\
        .clip(0, 255).astype(np.uint8)


class LiveAudioVideoTexture:
    def __init__(
        self, controls: PaletteControls, video_controls: VideoPlaybackControls,
        latest_samples: Callable[[int], np.ndarray] | None, *, size: int = 48,
        sensitivity: float = 1.5, immediate: bool = False,
    ) -> None:
        self.controls = controls
        self.video_controls = video_controls
        self.latest_samples = latest_samples
        if latest_samples is None:
            video_controls.set_layers({"audio_palette": False})
        self.size = size
        self.sensitivity = sensitivity
        self.immediate = immediate
        # 21 ms of spectrum history instead of 43 ms; amplitude uses only
        # the newest 5 ms below, so transients do not wait for a full FFT.
        self.energy = AdaptiveAudioEnergy(fft_size=1_024)
        self.settings = default_settings() | {"preset": "adaptive", "brightness": 1.0}
        self.revision = -1
        self.phase = 0.0
        self.wave_phase = 0.0
        self.pulse = 0.0
        self.visual_energy = 0.0
        self.band_levels = np.zeros(3, dtype=np.float32)
        self.mapping_envelope = np.zeros(5, dtype=np.float32)
        self.mapping_clock = 0.0
        self.mapping_history: deque[tuple[float, np.ndarray]] = deque()
        self.last_time: float | None = None
        self.palette = np.zeros((size, 3), dtype=np.uint8)
        y, x = np.indices((size, size), dtype=np.float32)
        center = (size - 1) / 2.0
        self.radius = np.hypot(x - center, y - center)
        self.angle = np.arctan2(y - center, x - center)
        self.max_radius = float(self.radius.max())
        # A small central core responds on this very frame. Quadratic travel
        # time makes the front slow down as it moves toward the corners.
        self.travel_position = np.clip(
            (self.radius - size * 0.055) / (self.max_radius - size * 0.055), 0.0, 1.0,
        ) ** 2
        radial_position = self.radius / (size / 2.0)
        self.band_regions = np.exp(-0.5 * (
            (radial_position[..., None] - np.array([0.85, 0.52, 0.15]))
            / np.array([0.26, 0.24, 0.23])
        ) ** 2).astype(np.float32)
        self.top_wave_position = np.clip((self.angle + np.pi) / np.pi, 0.0, 1.0)
        self.top_wave_region = (
            np.maximum(-np.sin(self.angle), 0.0) ** 2
            * np.exp(-0.5 * ((radial_position - 0.85) / 0.18) ** 2)
        ).astype(np.float32)

    def _top_wave_radius(self, samples: np.ndarray, strength: float) -> np.ndarray:
        """Bend the upper outer ring using the current audio waveform."""
        if strength <= 0.0 or len(samples) < 2:
            return self.radius
        # Average short sample groups before drawing them on LEDs. This keeps
        # bass contours readable and filters treble too fine for the panel.
        tail = samples[-self.energy.fft_size:]
        waveform = np.interp(
            np.linspace(0, len(tail) - 1, 1024), np.arange(len(tail)), tail,
        ).reshape(64, 16).mean(axis=1)
        waveform -= waveform.mean()
        waveform = np.convolve(np.pad(waveform, (2, 2), mode="edge"),
                               [0.1, 0.2, 0.4, 0.2, 0.1], mode="valid")
        peak = float(np.max(np.abs(waveform)))
        if peak < 1e-8:
            return self.radius
        contour = np.interp(self.top_wave_position, np.linspace(0, 1, 64),
                            waveform / peak)
        displacement = self.size * 0.025 * strength * self.top_wave_region * contour
        return self.radius - displacement

    def _mapping_fields(self, targets: np.ndarray, elapsed: float,
                        dt: float) -> np.ndarray:
        """Immediate central attack, soft release, then outward transport."""
        self.mapping_clock += max(0.0, elapsed)
        travel_time = 0.70 + 0.70 * self.settings["slowdown"] / 95.0
        release_time = 0.42 + 0.18 * self.settings["slowdown"] / 95.0
        self.mapping_envelope[:] = targets + np.maximum(
            0.0, self.mapping_envelope - targets,
        ) * np.exp(-dt / release_time)
        self.mapping_envelope[self.mapping_envelope < 0.002] = 0.0
        entry = (self.mapping_clock, self.mapping_envelope.copy())
        if self.mapping_history and self.mapping_history[-1][0] == self.mapping_clock:
            self.mapping_history[-1] = entry
        else:
            self.mapping_history.append(entry)
        # Retain one older point so interpolation remains continuous at the
        # farthest pixel. Slowdown edits can use up to 1.4 s of history.
        while (len(self.mapping_history) > 2
               and self.mapping_history[1][0] < self.mapping_clock - 1.4):
            self.mapping_history.popleft()
        times = np.array([entry[0] for entry in self.mapping_history])
        values = np.stack([entry[1] for entry in self.mapping_history])
        delay = travel_time * self.travel_position
        local_time = self.mapping_clock - delay
        feather = np.minimum(0.045, delay * 0.5)
        return np.stack([
            0.25 * np.interp(local_time - feather, times, values[:, channel], left=0.0)
            + 0.50 * np.interp(local_time, times, values[:, channel], left=0.0)
            + 0.25 * np.interp(local_time + feather, times, values[:, channel], left=0.0)
            for channel in range(5)
        ], axis=-1)

    def apply(self, frame: np.ndarray, *,
              select_frame: Callable[[np.ndarray], np.ndarray] | None = None) -> np.ndarray:
        now = time.monotonic()
        elapsed = 0.0 if self.last_time is None else max(0.0, now - self.last_time)
        self.last_time = now
        samples = (self.latest_samples(self.energy.fft_size)
                   if self.latest_samples is not None else np.empty(0))
        return self.render(frame, samples, elapsed, select_frame=select_frame)

    def render(self, frame: np.ndarray, samples: np.ndarray, elapsed: float, *,
               select_frame: Callable[[np.ndarray], np.ndarray] | None = None) -> np.ndarray:
        if frame.shape != (self.size, self.size, 3):
            raise ValueError("video texture requires a native-size RGB frame")
        layers = self.video_controls.output_layers()
        if not layers["audio_palette"] or self.latest_samples is None:
            self.mapping_envelope.fill(0.0)
            self.mapping_history.clear()
            layers["audio_palette"] = False
            self.video_controls.set_content_fps(self.video_controls.fps)
            if self.latest_samples is not None:
                self.video_controls.set_audio_reactivity(0.0, 0.0, False)
            frame = frame if select_frame is None else select_frame(frame)
            return compose_output_layers(frame, np.zeros_like(frame), layers)
        revision, settings = self.controls.settings_snapshot()
        if revision != self.revision:
            if settings["preset"] != self.settings["preset"]:
                self.energy.reset()
                self.visual_energy = 0.0
                self.phase = 0.0
                self.mapping_envelope.fill(0.0)
                self.mapping_history.clear()
            self.revision, self.settings = revision, settings
        energy_target, _colorful = self.energy.update(
            samples, elapsed, sensitivity=self.sensitivity,
            immediate=True,
        )
        recent = samples[-256:]
        audio_level = (float(np.sqrt(np.mean(np.square(recent, dtype=np.float64))))
                       * self.sensitivity if len(recent) else 0.0)
        audible = audio_level >= 0.0005
        if not audible:
            energy_target = 0.0
        # Smooth controls rather than video frames. Beats attack on the current
        # send deadline; short releases reject dips between bass cycles.
        dt = min(max(elapsed, 0.0), 0.1) or 1.0 / self.video_controls.fps
        if self.immediate:
            self.visual_energy = energy_target
        else:
            tau = 0.012 if energy_target > self.visual_energy else 0.09
            self.visual_energy += (energy_target - self.visual_energy) * (-np.expm1(-dt / tau))
            if self.visual_energy < 0.005 and energy_target == 0.0:
                self.visual_energy = 0.0
        energy = self.visual_energy
        palette_settings = self.settings | {"brightness": 1.0, "saturation": 1.0}
        if self.settings["preset"] == "adaptive":
            self.palette = make_video_energy_palette(
                energy, size=self.size, reverse=self.settings["reverse"],
            )
        else:
            if self.settings["preset"] == "moving-rainbow":
                self.phase = (self.phase + min(elapsed, 0.1) *
                              (1.0 - self.settings["slowdown"] / 100.0) / 8.0) % 1.0
            self.palette = make_palette(palette_settings, size=self.size, phase=self.phase)

        pulse_target = float(np.clip((audio_level - 0.002) / 0.09, 0.0, 1.0))
        self.pulse = (pulse_target if self.immediate else
                      pulse_target + max(0.0, self.pulse - pulse_target) * np.exp(-dt / 0.05))
        amplitudes = self.energy.band_amplitudes
        dominant = float(np.max(amplitudes))
        band_targets = (pulse_target * (amplitudes / dominant) ** 1.5
                        if dominant > 0 else np.zeros(3))
        if self.immediate:
            self.band_levels[:] = band_targets
        else:
            self.band_levels[:] = band_targets + np.maximum(
                0.0, self.band_levels - band_targets,
            ) * np.exp(-dt / 0.06)
            if pulse_target == 0.0:
                if self.pulse < 0.005:
                    self.pulse = 0.0
                self.band_levels[self.band_levels < 0.005] = 0.0
        fields = self._mapping_fields(
            np.r_[energy_target, pulse_target, band_targets], elapsed, dt,
        )
        local_energy, local_pulse = fields[..., 0], fields[..., 1]
        # The host holds decoded video frames during calm, bass-light music.
        # Audio texture and USB output still refresh at the normal panel rate.
        content_min_fps = min(10.0, self.video_controls.fps)
        activity = min(1.0, 0.60 * energy + 0.40 * float(self.band_levels[0]))
        desired_fps = (content_min_fps
                       + (self.video_controls.fps - content_min_fps) * activity)
        self.video_controls.set_content_fps(desired_fps)
        if select_frame is not None:
            # Choose video motion using the same current audio as the mapping.
            frame = select_frame(frame)
        drive = min(1.0, 0.40 * energy + 0.30 * self.pulse
                    + 0.30 * float(self.band_levels.max()))
        tempo = 1.0 - 0.80 * self.settings["slowdown"] / 95.0
        self.wave_phase = (
            self.wave_phase + min(elapsed, 0.1) * drive * (0.03 + 0.17 * drive)
            * tempo * 2.0 * np.pi
        ) % (2.0 * np.pi)
        # Keep the upper waveform responsive but shallow; the main color and
        # opacity change travels outward using the transported fields below.
        mapped_radius = self._top_wave_radius(samples, pulse_target if audible else 0.0)
        spatial_frequency = 2.0 * np.pi / (self.size * 0.35)
        base = mapped_radius * spatial_frequency - self.wave_phase
        # A quieter returning wave meets the outgoing ring at the edge. Its
        # second harmonic adds a soft shimmer without making another hard band.
        outgoing = np.sin(base + 0.12 * drive * np.sin(2.0 * self.angle))
        returning = np.sin((2.0 * self.max_radius - mapped_radius)
                           * spatial_frequency - self.wave_phase)
        harmonic = np.sin(2.0 * base + 0.18 * np.cos(2.0 * self.angle))
        wave = 0.70 * outgoing + 0.20 * returning + 0.10 * harmonic
        outer_region = np.exp(-0.5 * ((mapped_radius / (self.size / 2.0) - 0.85)
                                    / 0.26) ** 2)
        bass = fields[..., 2] * outer_region
        mids = fields[..., 3] * self.band_regions[..., 1]
        highs = fields[..., 4] * self.band_regions[..., 2]
        motion = np.maximum.reduce((1.40 * bass, 0.85 * mids, 0.95 * highs))
        crest = np.maximum(wave, 0.0)

        palette_position = np.clip(
            mapped_radius / self.max_radius * (self.size - 1) + 2.5 * motion * wave,
            0, self.size - 1,
        )
        palette_low = np.floor(palette_position).astype(np.intp)
        palette_high = np.minimum(palette_low + 1, self.size - 1)
        palette_mix = (palette_position - palette_low)[..., None]
        if self.settings["preset"] == "adaptive":
            stages = _video_energy_stages(self.size)
            if self.settings["reverse"]:
                stages = stages[:, ::-1]
            sampled_stages = (stages[:, palette_low] * (1.0 - palette_mix)
                              + stages[:, palette_high] * palette_mix)
            circular_palette = _spatial_energy_colors(sampled_stages, local_energy)
        else:
            circular_palette = (
                self.palette[palette_low] * (1.0 - palette_mix)
                + self.palette[palette_high] * palette_mix
            )
        # This RGB layer depends only on audio and palette settings, so it
        # remains visible when the video layer is hidden or the source is black.
        colored = circular_palette * 0.55
        glow = crest * (0.18 * bass + 0.08 * mids + 0.12 * highs)
        colored += circular_palette * glow[..., None]
        luma = np.sum(colored * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32),
                      axis=2, keepdims=True)
        saturation_base = (0.85 + 0.15 * local_energy if self.settings["preset"] == "adaptive"
                           else 0.35 + 0.65 * local_energy)
        saturation = self.settings["saturation"] * saturation_base
        # Video intensity is gamma-mapped later. Invert that curve here so a
        # saved audio brightness value is applied once to the palette layer.
        gain = self.settings["brightness"] ** (1.0 / 2.2) * (1.0 + 0.55 * local_energy)
        result = luma + (colored - luma) * saturation[..., None]
        shine = crest * (0.22 * bass + 0.08 * mids + 0.14 * highs)
        result = np.rint(result * (gain * (
            1.0 + 0.04 * motion * wave + shine
        ))[..., None])\
            .clip(0, 255).astype(np.uint8)
        center_saturation = self.settings["saturation"] * (
            0.85 + 0.15 * energy if self.settings["preset"] == "adaptive"
            else 0.35 + 0.65 * energy
        )
        self.video_controls.set_audio_reactivity(energy, center_saturation, audible)
        # Transparency carries the same expanding envelope as color and glow.
        opacity = VIDEO_OVERLAY_OPACITY * (1.0 + 0.12 * local_pulse)
        return compose_output_layers(frame, result, layers, opacity=opacity)

    def publish_frame(self, frame: np.ndarray) -> None:
        self.controls.publish_frame(frame, self.palette)
