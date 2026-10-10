"""Track a prominent tonal line for the circular melody-string overlay."""

from __future__ import annotations

import numpy as np


class MelodyFFT:
    """A tonal-salience heuristic, rather than instrument/source separation.

    Search the midrange for peaks above their local noise floor, reinforce
    harmonic families, and favor the current note when two peaks nearly tie.
    Retain only that note and its first two overtones for the string contour.
    """

    def __init__(self, sample_rate: int = 48_000, fft_size: int = 4_096) -> None:
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
        self._candidates = np.flatnonzero(
            (self.frequencies >= 180.0) & (self.frequencies <= 3_000.0),
        )
        self._window = np.hanning(fft_size)
        self.reset()

    def reset(self) -> None:
        self.frequency = 0.0
        self.pitch = 0.0
        self.strength = 0.0
        self.confidence = 0.0
        self.harmonics = np.zeros(3, dtype=np.complex128)
        self.filtered_spectrum = np.zeros(len(self.frequencies), dtype=np.complex128)
        self._salience = np.zeros(len(self.frequencies))
        self._selected_bin: int | None = None

    def _release(self, dt: float) -> None:
        self.strength *= np.exp(-dt / 0.10)
        self._salience *= np.exp(-dt / 0.07)
        self.filtered_spectrum.fill(0.0)
        self.confidence = 0.0
        if self.strength < 0.002:
            self.reset()

    def update(self, samples: np.ndarray, elapsed: float, *,
               sensitivity: float = 1.5, audible: bool = True) -> None:
        dt = min(max(elapsed, 0.0), 0.1) or 1.0 / 60.0
        tail = np.asarray(samples[-self.fft_size:], dtype=np.float64)
        if not audible or len(tail) < 128:
            self._release(dt)
            return
        if len(self._window) != len(tail):
            self._window = np.hanning(len(tail))
        signal = (tail - tail.mean()) * self._window
        spectrum = np.fft.rfft(signal, n=self.fft_size)
        magnitude = np.abs(spectrum)
        peak = float(magnitude.max())
        if peak < 1e-9:
            self._release(dt)
            return
        power = magnitude[self._candidates] ** 2
        flatness = float(np.exp(np.log(np.maximum(power, 1e-20)).mean())
                         / max(float(power.mean()), 1e-20))
        # Broadband noise and clicks can have isolated random peaks, but they
        # should not produce a melody. Tonal signals have low spectral flatness.
        if flatness >= 0.45:
            self._release(dt)
            return

        # Adapt the neighborhood to the actual input length, including startup
        # and shorter offline render buffers. Zero-padding adds no resolution.
        width = max(8, round(6 * self.fft_size / len(tail)))
        neighborhoods = np.lib.stride_tricks.sliding_window_view(
            np.pad(magnitude, (width, width), mode="edge"), 2 * width + 1,
        )
        # Only the candidate notes and their first three harmonics use the
        # local floor. Include their full search bandwidth and interpolation
        # neighbors; high treble still participates in flatness/peak rejection.
        highest = self.frequencies[self._candidates[-1]] + self.sample_rate / self.fft_size
        bandwidth_limit = max(1.5 * self.sample_rate / len(tail), highest * 0.03)
        limit = min(len(magnitude), int(np.searchsorted(
            self.frequencies, highest * 3.0 + bandwidth_limit,
        )) + 2)
        tonal = np.zeros_like(magnitude)
        floor = np.median(neighborhoods[:limit], axis=-1)
        tonal[:limit] = np.maximum(magnitude[:limit] - 3.0 * floor, 0.0)
        harmonic_tonal = np.maximum.reduce((np.roll(tonal, -1), tonal, np.roll(tonal, 1)))
        bins = self._candidates
        scores = tonal[bins].copy()
        for harmonic, weight in ((2, 0.65), (3, 0.40)):
            scores += weight * np.interp(
                self.frequencies[bins] * harmonic, self.frequencies, harmonic_tonal,
            )
        # Require a real fundamental. This rejects subharmonics invented by
        # summing overtones and small sidelobes of a much louder kick/bass note.
        peaks = ((magnitude[bins] > magnitude[bins - 1])
                 & (magnitude[bins] >= magnitude[bins + 1])
                 & (magnitude[bins] >= peak * 0.025))
        scores *= peaks * tonal[bins] / np.maximum(magnitude[bins], 1e-12)
        blend = -np.expm1(-dt / 0.07)
        self._salience[bins] += (scores - self._salience[bins]) * blend
        ranked = 0.65 * scores + 0.35 * self._salience[bins]
        # A vanished peak must not beat the newly playing note on history alone.
        ranked[scores <= 0.0] = 0.0
        best = int(np.argmax(ranked))
        if ranked[best] <= 0.0:
            self._release(dt)
            return
        if self._selected_bin is not None:
            current = int(np.searchsorted(bins, self._selected_bin))
            if current < len(bins) and ranked[current] >= 0.85 * ranked[best]:
                best = current
        selected = int(bins[best])

        # Log-parabolic interpolation improves pitch between FFT bins.
        left, middle, right = np.log(np.maximum(magnitude[selected - 1:selected + 2], 1e-12))
        curvature = left - 2.0 * middle + right
        offset = (float(np.clip(0.5 * (left - right) / curvature, -0.5, 0.5))
                  if abs(curvature) > 1e-12 else 0.0)
        frequency = (selected + offset) * self.sample_rate / self.fft_size
        confidence = float(np.clip(tonal[selected] / magnitude[selected], 0.0, 1.0))
        amplitude = 2.0 * magnitude[selected] / self._window.sum()
        target = float(np.clip(amplitude * sensitivity / 0.065, 0.0, 1.0)) * confidence
        if target < 0.008:
            self._release(dt)
            return

        bandwidth = max(1.5 * self.sample_rate / len(tail), frequency * 0.03)
        mask = np.zeros(len(spectrum))
        harmonics = np.zeros(3, dtype=np.complex128)
        fundamental_phase = np.angle(spectrum[selected])
        for index in range(3):
            harmonic = index + 1
            center = frequency * harmonic
            mask = np.maximum(mask, np.exp(-0.5 * (
                (self.frequencies - center) / bandwidth
            ) ** 2))
            nearby = np.flatnonzero(np.abs(self.frequencies - center) <= bandwidth)
            harmonic_bin = int(nearby[np.argmax(magnitude[nearby])])
            amount = max(0.0, float(tonal[harmonic_bin]))
            # Anchor phase to the fundamental so capture-window phase does
            # not spin or flicker the whole string between transmitted frames.
            phase = np.angle(spectrum[harmonic_bin]) - harmonic * fundamental_phase
            harmonics[index] = amount * np.exp(1j * phase)
        mask[self.frequencies < 180.0] = 0.0
        self.filtered_spectrum = spectrum * mask
        harmonics /= max(float(np.abs(harmonics).sum()), 1e-12)
        if self._selected_bin is None:
            self.harmonics[:] = harmonics
        else:
            self.harmonics += (harmonics - self.harmonics) * (-np.expm1(-dt / 0.08))
        pitch = float(np.clip(np.log(frequency / 180.0) / np.log(3000.0 / 180.0), 0.0, 1.0))
        self.pitch = (pitch if self._selected_bin is None else
                      self.pitch + (pitch - self.pitch) * (-np.expm1(-dt / 0.08)))
        self._selected_bin = selected
        self.frequency = frequency
        self.confidence = confidence
        # Notes attack immediately, with a brief release between syllables.
        self.strength = target + max(0.0, self.strength - target) * np.exp(-dt / 0.10)

    def contour(self, angle: np.ndarray, rotation: float = 0.0) -> np.ndarray:
        """Map the filtered harmonics onto two smooth cycles of a closed loop."""
        result = np.zeros_like(angle)
        for index, coefficient in enumerate(self.harmonics):
            result += abs(coefficient) * np.cos(
                2.0 * (index + 1) * (angle - rotation) + np.angle(coefficient),
            )
        return result
