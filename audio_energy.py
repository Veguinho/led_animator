"""Shared music-energy envelope for the audio visualizer and video texture."""

from __future__ import annotations

from collections import deque

import numpy as np


class AdaptiveAudioEnergy:
    """Track how energetic the current passage is relative to recent music."""

    def __init__(self, sample_rate: int = 48_000, fft_size: int = 2_048) -> None:
        self.fft_size = fft_size
        self.window = np.hanning(fft_size).astype(np.float32)
        frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
        self.masks = [
            (frequencies >= low) & (frequencies < high)
            for low, high in ((45, 250), (250, 2500), (2500, 16000))
        ]
        self.reset()

    def reset(self) -> None:
        self.energy = 0.0
        self.colorful = 0.0
        self.history: deque[tuple[float, float, float]] = deque()
        self.clock = 0.0
        self.song_levels: np.ndarray | None = None

    def update(
        self, samples: np.ndarray, elapsed: float, *, sensitivity: float = 1.5,
        slowdown: float = 20.0,
    ) -> tuple[float, float]:
        self.clock += max(0.0, elapsed)
        while self.history and self.history[0][0] < self.clock - 30.0:
            self.history.popleft()
        signal = np.zeros(self.fft_size, dtype=np.float64)
        tail = samples[-self.fft_size:]
        if len(tail):
            signal[-len(tail):] = tail
        signal -= signal.mean()
        power = np.abs(np.fft.rfft(signal * self.window)) ** 2
        energies = np.array([power[mask].sum() for mask in self.masks])
        energies *= 2.0 / (self.fft_size * np.square(self.window).sum())
        amplitudes = np.sqrt(energies)
        total = float(energies.sum())
        levels = np.array([total, float(np.max(np.abs(signal))) ** 2]) * sensitivity**2
        if self.song_levels is None:
            self.song_levels = levels
        else:
            self.song_levels += (levels - self.song_levels) * (
                -np.expm1(-min(elapsed, 0.1) / 0.25)
            )
        energy = colorful = 0.0
        if np.sqrt(total) * sensitivity >= 0.0005:
            relative_db = 20.0 * np.log10(max(float(amplitudes.min() / amplitudes.max()), 1e-12))
            presence = float(np.clip((relative_db + 42.0) / 18.0, 0, 1))
            presence = presence * presence * (3.0 - 2.0 * presence)
            rms_db, peak_db = 10.0 * np.log10(np.maximum(self.song_levels, 1e-12))
            self.history.append((self.clock, rms_db, peak_db))
            rms_max = max(-36.0, max(item[1] for item in self.history))
            peak_max = max(-30.0, max(item[2] for item in self.history))
            relative_db = 0.8 * (rms_db - rms_max) + 0.2 * (peak_db - peak_max)
            amount = float(np.clip((relative_db + 6.0) / 4.0, 0, 1))
            audible = float(np.clip((rms_db + 60.0) / 18.0, 0, 1))
            energy = amount * amount * (3.0 - 2.0 * amount) * audible
            colorful = energy * (0.8 + 0.2 * presence)
        dt = min(elapsed, 0.1) / (1.0 + 3.0 * slowdown / 95.0)
        tau = 0.8 if energy > self.energy else 1.4
        self.energy += (energy - self.energy) * (-np.expm1(-dt / tau))
        self.colorful += (colorful - self.colorful) * (-np.expm1(-dt / tau))
        return self.energy, self.colorful
