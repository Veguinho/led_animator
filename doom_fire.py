"""Music-driven Doom fire, based on Filipe Deschamps' Doom Fire Algorithm.

The propagation algorithm is adapted under the MIT license in
docs/licenses/doom-fire-algorithm.txt. The palette shifts from blue/purple
embers to red/orange heat and restrained yellow highlights, without white.
"""

from __future__ import annotations

import threading

import numpy as np

from audio_energy import AdaptiveAudioEnergy


FIRE_PALETTE = np.array([
    (0, 0, 0), (24, 0, 0), (36, 0, 0), (48, 0, 0), (60, 0, 0),
    (72, 0, 0), (84, 0, 0), (96, 0, 0), (108, 0, 0), (120, 0, 0),
    (132, 0, 0), (144, 0, 0), (156, 0, 0), (168, 0, 0), (180, 0, 0),
    (188, 0, 0), (196, 0, 0), (204, 1, 0), (212, 2, 0), (220, 3, 0),
    (226, 4, 0), (232, 5, 0), (238, 6, 0), (242, 8, 0), (246, 10, 0),
    (250, 18, 0), (253, 28, 0), (255, 40, 0), (255, 56, 0),
    (255, 76, 0), (255, 98, 0), (255, 120, 0), (255, 143, 0),
    (255, 164, 0), (255, 184, 0), (255, 200, 0), (255, 216, 0),
], dtype=np.uint8)

# Follow the Adaptive effect's cool-to-warm passage colors, but color by
# heat within the flame so its tips and hot base remain distinct.
_COOL_STOPS = np.array([
    (0, 0, 0), (8, 0, 32), (48, 4, 128), (50, 12, 196),
    (32, 28, 232), (24, 56, 255),
], dtype=np.float32)
COOL_FIRE_PALETTE = np.column_stack([
    np.interp(np.arange(37), [0, 3, 10, 18, 27, 36], _COOL_STOPS[:, channel])
    for channel in range(3)
]).astype(np.float32)


def fire_palette_for_energy(energy: float) -> np.ndarray:
    """Ease cool-to-warm colors across a broad passage-energy range."""
    warmth = float(np.clip((energy - 0.1) / 0.8, 0, 1))
    warmth = warmth**3 * (warmth * (warmth * 6 - 15) + 10)
    return COOL_FIRE_PALETTE * (1 - warmth) + FIRE_PALETTE * warmth


def visible_cold_fire_colors(colors: np.ndarray, brightness: float) -> np.ndarray:
    """Keep blue and purple above the RGB565 and firmware PWM thresholds.

    Red/blue use 8-byte wire steps, then the firmware scales by 60/255.
    A blue byte of 16 survives as PWM 3; a red byte of 8 preserves purple's
    red component. Apply this only to colored cold pixels, leaving black,
    warm flames and the user's channel brightness ceiling intact.
    """
    rgb = colors.astype(np.float32)
    peak = rgb.max(axis=-1)
    hue_strength = np.clip((rgb[..., 2] / np.maximum(peak, 1e-6) - 0.25) / 0.5, 0, 1)
    hue_strength = hue_strength * hue_strength * (3 - 2 * hue_strength)
    hue_strength *= np.clip((0.6 - rgb[..., 1] / np.maximum(peak, 1e-6)) / 0.2, 0, 1)
    # Do not revive sub-byte residue while the fire is extinguishing.
    cold = (peak >= 1) & (hue_strength > 0)
    steps = np.array([8, 4, 8], dtype=np.float32)
    quantized = np.floor(rgb / steps + 0.5) * steps
    quantized[..., 2] = np.maximum(quantized[..., 2], 16 * hue_strength)
    purple = rgb[..., 0] >= rgb[..., 2] * 0.2
    quantized[..., 0] = np.where(purple, np.maximum(quantized[..., 0], 8 * hue_strength), quantized[..., 0])
    quantized = np.floor(quantized / steps + 0.5) * steps
    ceiling = np.floor(round(brightness * 255) / steps) * steps
    quantized = np.minimum(quantized, ceiling)
    return np.rint(np.where(cold[..., None], quantized, rgb)).clip(0, 255).astype(np.uint8)


class DoomFireControls:
    """Atomic effect selection and telemetry shared by HTTP and rendering."""

    def __init__(self, active: bool = False) -> None:
        self._lock = threading.Lock()
        self._state = {"active": active, "energy": 0.0, "height": 0.0, "audible": False}

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._state = {"active": active, "energy": 0.0, "height": 0.0, "audible": False}

    def state(self) -> dict:
        with self._lock:
            return self._state.copy()

    def publish(self, energy: float, height: float, audible: bool) -> None:
        with self._lock:
            self._state.update(energy=energy, height=height, audible=audible)


class DoomFire:
    """Propagate heat upward with random sideways drift and cooling.

    Audio supplies hotter fuel, so energetic passages produce taller,
    brighter flames. Wind and cooling retain the original coupled decay
    rule, scaled vertically from the reference's 40 rows.
    """

    def __init__(self, size: int = 48, *, seed: int | None = None) -> None:
        if size not in (16, 32, 48):
            raise ValueError("display size must be 16, 32 or 48")
        self.size = size
        self.rng = np.random.default_rng(seed)
        self.tracker = AdaptiveAudioEnergy()
        self.heat = np.zeros((size, size), dtype=np.float32)
        self.energy = 0.0
        self.color_energy = 0.0
        self.ember_clock = 0.0
        self.audible = False
        self.phase = 0.0
        self._burst_age = 1.0
        self._burst_strength = self._burst_progress = 0.0
        self._burst_color_start = self._burst_color_target = 0.0
        self.palette = FIRE_PALETTE.copy()
        self._base_rows = max(2, round(size * 0.1))
        self._base_blend = np.linspace(0.15, 0.95, self._base_rows)[:, None, None]
        # Keep the original column-then-row traversal. Later writes win when
        # wind sends two pixels to the same destination; untouched pixels
        # retain their heat. Both details create the original flame tongues.
        self._indices = (np.arange(size - 1)[None, :] * size
                         + np.arange(size)[:, None])
        self._scatter_winners = np.empty(size * size, dtype=np.intp)
        heat_level = np.arange(37) / 36
        self._warm_intensity = 0.12 + 0.88 * heat_level**1.25
        self._cold_intensity = 0.3 + 0.7 * heat_level**0.6
        self._base_phase = 2 * np.pi * np.arange(size) * 3 / size

    def reset(self) -> None:
        self.tracker.reset()
        self.heat.fill(0)
        self.energy = self.phase = 0.0
        self.color_energy = self.ember_clock = 0.0
        self._burst_age = 1.0
        self._burst_strength = self._burst_progress = 0.0
        self.audible = False

    def render(self, samples: np.ndarray, elapsed: float, *, sensitivity: float = 1.5,
               brightness: float = 16 / 255, saturation: float = 1.0,
               slowdown: float = 20.0) -> np.ndarray:
        dt = min(max(elapsed, 0.0), 0.25)
        relative, _ = self.tracker.update(samples, dt, sensitivity=sensitivity,
                                          slowdown=slowdown, immediate=True)
        # Detect attacks in the latest 10.7 ms instead of averaging the
        # whole 42.7 ms spectrum window. The FFT still supplies band fullness.
        tail = samples[-512:]
        # Remove DC, just as the shared energy tracker does.
        signal = tail.astype(np.float64) - tail.mean() if len(tail) else tail
        rms = float(np.sqrt(np.mean(signal * signal))) * sensitivity if len(tail) else 0.0
        self.audible = rms >= 0.0005
        absolute = float(np.clip((20.0 * np.log10(max(rms, 1e-12)) + 54) / 44, 0, 1))
        absolute = absolute * absolute * (3.0 - 2.0 * absolute)
        amplitudes = self.tracker.band_amplitudes
        fullest = float(amplitudes.min() / max(float(amplitudes.max()), 1e-12))
        fullness = float(np.clip((20.0 * np.log10(max(fullest, 1e-12)) + 36) / 18, 0, 1))
        target = absolute * (0.55 + 0.45 * relative) * (0.6 + 0.4 * fullness) if self.audible else 0.0
        jump = target - self.energy
        if (jump > 0.3 and target > 0.55 and self.color_energy < 0.55
                and self._burst_age >= 0.45):
            self._burst_age = self._burst_progress = 0.0
            self._burst_strength = min(1.0, jump / 0.75)
            self._burst_color_start = self.color_energy
            self._burst_color_target = target
        tau = 0.012 if target > self.energy else 0.65
        self.energy += (target - self.energy) * (-np.expm1(-dt / tau))
        # Ordinary passages ease across beats. A sudden cold-to-hot attack
        # pumps extra rise into 320 ms, with a fast logarithmic color sweep.
        # Only scalar envelopes ease; flame edges retain their crisp pixels.
        color_dt = dt / (1 + 2 * slowdown / 95)
        color_tau = 1.2 if self.energy > self.color_energy else 1.8
        self.color_energy += (self.energy - self.color_energy) * (-np.expm1(-color_dt / color_tau))
        self._burst_age += dt
        burst_progress = float(np.log1p(9 * min(self._burst_age / 0.32, 1)) / np.log(10))
        burst_rise = self.size * 0.4 * self._burst_strength * (burst_progress - self._burst_progress)
        if self._burst_strength and self._burst_progress < 1:
            self.color_energy = max(self.color_energy, self._burst_color_start
                + (self._burst_color_target - self._burst_color_start) * burst_progress)
            if self.audible:
                # The air pulse heats existing tongues before carrying them
                # upward, rather than waiting for hot fuel to climb from zero.
                burning = self.heat >= 1
                self.heat[burning] = np.minimum(36, self.heat[burning]
                    + 18 * self._burst_strength * (burst_progress - self._burst_progress))
        self._burst_progress = burst_progress
        self.ember_clock += dt * (1 - slowdown / 100)
        if not self.audible:
            # Embers cool even when the motion slider nearly freezes the rise.
            self.heat *= np.exp(-dt / 0.4)
        # Slowdown governs motion; the music envelope still updates each frame.
        # The reference advances every 50 ms at 40 rows. Scale vertical rise
        # to the native grid while keeping its wind independent of audio.
        self.phase += dt * (20 * self.size / 40) * (1 - slowdown / 100) + burst_rise
        while self.phase >= 1.0:
            self.phase -= 1.0
            self._step()
        # The firmware caps each pixel's RGB sum at 64 before PWM scaling.
        # Budget intensity within that range: sending saturated reds above
        # the cap made most of the flame equally bright on the actual LEDs.
        cold_weight = float(np.clip((0.8 - self.color_energy) / 0.5, 0, 1))
        cold_weight = cold_weight * cold_weight * (3 - 2 * cold_weight)
        budget_gain = (0.4 + 0.6 * self.energy) * (1 - cold_weight) + 0.8 * cold_weight
        budget = min(64.0, brightness * 255) * budget_gain
        palette = fire_palette_for_energy(self.color_energy)
        neutral = palette.max(axis=1, keepdims=True)
        palette = neutral + (palette - neutral) * saturation
        # Cold low-energy flames occupy the lower heat indices too. The
        # warm contrast curve pushed most of their blue values below one
        # wire step. Lift only the cold curve so its contours remain visible.
        intensity = self._warm_intensity * (1 - cold_weight) + self._cold_intensity * cold_weight
        intensity[0] = 0
        palette *= (budget * intensity / np.maximum(palette.sum(axis=1), 1))[:, None]
        self.palette = visible_cold_fire_colors(palette, brightness)
        frame = self.palette[np.rint(self.heat).clip(0, 36).astype(np.intp)].astype(np.float32)
        self._color_base(frame, budget, saturation)
        # The main flame already uses the wire-aware LUT; only the ember
        # bed needs a second conversion after its colors have been blended.
        frame[-self._base_rows:] = visible_cold_fire_colors(frame[-self._base_rows:], brightness)
        return frame.astype(np.uint8)

    def _color_base(self, frame: np.ndarray, budget: float, saturation: float) -> None:
        """Keep a cool ember bed, adding yellow pools as the song builds."""
        wave = 0.5 + 0.5 * np.sin(self._base_phase + self.ember_clock * 0.9)
        cool = np.column_stack((104 + 40 * wave, 28 - 20 * wave, 255 - 24 * wave))
        yellow_drive = float(np.clip((self.color_energy - 0.45) / 0.5, 0, 1))
        yellow_drive = yellow_drive * yellow_drive * (3 - 2 * yellow_drive)
        # Some columns stay blue/purple even at full energy. Hot spots drift
        # left along the bed and gradually brighten yellow in between them.
        hot = (0.95 * yellow_drive * np.clip((wave - 0.35) / 0.45, 0, 1))[:, None]
        colors = cool * (1 - hot) + np.array([255, 200, 0]) * hot
        neutral = colors.max(axis=1, keepdims=True)
        colors = neutral + (colors - neutral) * saturation
        colors /= np.maximum(colors.sum(axis=1, keepdims=True), 1)
        heat = self.heat[-self._base_rows:]
        intensity = (0.7 + 0.3 * heat / 36) * np.clip(heat / 6, 0, 1)
        bed = colors[None, :, :] * (budget * intensity[..., None])
        frame[-self._base_rows:] *= 1 - self._base_blend
        frame[-self._base_rows:] += bed * self._base_blend

    def _step(self) -> None:
        fuel = (10 + 26 * self.energy) if self.audible else 0.0
        self.heat[-1] = fuel
        # Like currentPixelIndex - decay in the original: 0, 1 or 2 pixels
        # left, and that same decay cools the pixel. Do not gather neighbors
        # or wrap the horizontal coordinates; that changes its wind pattern.
        # Scaling only heat loss preserves the 40-row reference's flame
        # height and tip shape instead of reducing cooling as music rises.
        decays = self.rng.integers(0, 3, size=self._indices.shape)
        cooling = 40 / self.size
        heat = self.heat.ravel()
        # Leftward writes cannot touch a later column's source, except the
        # first two columns wrapping into the last two. Batch the main grid,
        # then those final columns. Explicit last-writer selection retains
        # the original scatter collisions without a Python loop per pixel.
        for indices, decay in ((self._indices[:-2], decays[:-2]),
                               (self._indices[-2:], decays[-2:])):
            indices, decay = indices.ravel(), decay.ravel()
            destination = indices - decay
            valid = destination >= 0
            values = np.maximum(0, heat[indices + self.size].astype(np.float64) - decay * cooling)
            winners = self._scatter_winners
            winners.fill(-1)
            np.maximum.at(winners, destination[valid], np.flatnonzero(valid))
            written = np.flatnonzero(winners >= 0)
            heat[written] = values[winners[written]]

    def height(self) -> float:
        rows = np.flatnonzero(np.any(self.heat >= 1, axis=1))
        return (self.size - int(rows[0])) / self.size if len(rows) else 0.0
