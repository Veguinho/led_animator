#!/usr/bin/env python3
"""Render live Mac system audio at the LED screen's native resolution."""

from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
from brightness_envelope import BrightnessEnvelope

from audio_palette_controls import DEFAULT_SLOWDOWN, PaletteControls, PaletteServer, default_settings, make_adaptive_palette, make_palette, validate_slowdown
from export_arduino import encode_rgb565
from stream_arduino import (
    DEFAULT_PORT_WAIT,
    DEFAULT_STREAM_FPS,
    PACKET_CLEAR,
    STATUS_ACK,
    FrameSource,
    SerialConnection,
    add_display_argument,
    exchange_packet,
    handshake,
    open_serial,
    resolve_port,
    stream_frames,
)


ROOT = Path(__file__).resolve().parent
CAPTURE_SOURCE = ROOT / "macos_system_audio.swift"
CAPTURE_INFO = ROOT / "macos_system_audio.plist"
CAPTURE_BINARY = ROOT / ".build" / "system_audio_capture"
SAMPLE_RATE = 48_000
# Offline previews retain their original size; live rendering uses --display-size.
GRID_SIZE = 16
FFT_SIZE = 2_048
MIN_DBFS = -72.0
MAX_DBFS = -6.0
DEFAULT_SENSITIVITY = 1.5
RAINBOW_CYCLE_SECONDS = 8.0


def amplitude_to_level(amplitude: float, sensitivity: float = 1.0) -> float:
    """Map a linear audio amplitude to a perceptual 0..1 dBFS level."""
    if amplitude <= 0.0:
        return 0.0
    dbfs = 20.0 * np.log10(max(amplitude * sensitivity, 1e-12))
    return float(np.clip((dbfs - MIN_DBFS) / (MAX_DBFS - MIN_DBFS), 0.0, 1.0))


def build_capture_helper() -> Path:
    """Compile the tiny native helper when it is missing or out of date."""
    if sys.platform != "darwin":
        raise RuntimeError("live system-audio capture is supported only on macOS")
    for source in (CAPTURE_SOURCE, CAPTURE_INFO):
        if not source.is_file():
            raise RuntimeError(f"capture helper source is missing: {source}")
    if (
        CAPTURE_BINARY.is_file()
        and CAPTURE_BINARY.stat().st_mtime >= max(
            CAPTURE_SOURCE.stat().st_mtime, CAPTURE_INFO.stat().st_mtime
        )
    ):
        return CAPTURE_BINARY

    CAPTURE_BINARY.parent.mkdir(parents=True, exist_ok=True)
    print("Building the macOS system-audio helper (first run only)...", file=sys.stderr)
    result = subprocess.run(
        [
            "xcrun", "swiftc", "-O", str(CAPTURE_SOURCE),
            "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
            "-Xlinker", "__info_plist", "-Xlinker", str(CAPTURE_INFO),
            "-o", str(CAPTURE_BINARY),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"could not build the system-audio helper: {detail}")
    return CAPTURE_BINARY


class AudioCapture:
    """Read mono Float32 samples from the native Core Audio tap helper."""

    def __init__(self, capacity: int = SAMPLE_RATE) -> None:
        self._samples = np.zeros(capacity, dtype=np.float32)
        self._sample_count = 0
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._error: str | None = None

    def start(self, permission_timeout: float = 120.0) -> None:
        binary = build_capture_helper()
        self._process = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert self._process.stderr is not None
        ready, _, _ = select.select(
            [self._process.stderr], [], [], permission_timeout
        )
        if not ready:
            self.close()
            raise RuntimeError(
                "timed out waiting for System Audio Recording Only permission"
            )
        message = self._process.stderr.readline().decode(errors="replace").strip()
        if message != "READY":
            self.close()
            detail = message.removeprefix("error: ") or "capture helper stopped"
            raise RuntimeError(
                f"could not capture Mac system audio: {detail}. In System Settings, "
                "allow the app that launched the visualizer (Terminal for the "
                "command-line launcher) under Privacy & Security > Screen & "
                "System Audio Recording > System Audio Recording Only, then "
                "restart that app. Screen recording access is not required"
            )

        self._reader = threading.Thread(
            target=self._read_samples,
            name="system-audio-reader",
            daemon=True,
        )
        self._reader.start()

    def _read_samples(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        pending = b""
        while True:
            chunk = os.read(self._process.stdout.fileno(), 16_384)
            if not chunk:
                break
            pending += chunk
            usable = len(pending) - len(pending) % np.dtype(np.float32).itemsize
            if usable == 0:
                continue
            samples = np.frombuffer(pending[:usable], dtype=np.float32).copy()
            pending = pending[usable:]
            self._append(samples)

        if self._process.poll() not in (None, 0):
            assert self._process.stderr is not None
            detail = self._process.stderr.read().decode(errors="replace").strip()
            self._error = detail.removeprefix("error: ") or "audio capture stopped"

    def _append(self, samples: np.ndarray) -> None:
        with self._lock:
            if len(samples) >= len(self._samples):
                self._samples[:] = samples[-len(self._samples) :]
                self._sample_count = len(self._samples)
                return
            self._samples[:-len(samples)] = self._samples[len(samples) :]
            self._samples[-len(samples) :] = samples
            self._sample_count = min(
                len(self._samples), self._sample_count + len(samples)
            )

    def latest(self, count: int) -> np.ndarray:
        if self._error is not None:
            raise RuntimeError(self._error)
        with self._lock:
            available = min(count, self._sample_count)
            if available == 0:
                return np.zeros(count, dtype=np.float32)
            result = np.zeros(count, dtype=np.float32)
            result[-available:] = self._samples[-available:]
            return result

    def close(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        if self._reader is not None:
            self._reader.join(timeout=1)
        self._process = None


def color_palette() -> np.ndarray:
    """Return the startup palette before audio envelopes begin moving."""
    return make_palette(size=GRID_SIZE)


class FrameDelayer:
    """Advance fewer animation frames and blend at the caller's output cadence.

    Each target uses current audio, so slowing the motion never accumulates
    a backlog. A fractional phase keeps arbitrary percentages accurate.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._start: np.ndarray | None = None
        self._target: np.ndarray | None = None
        self._phase = 1.0

    def render(self, next_frame: Callable[[], np.ndarray], slowdown: float) -> np.ndarray:
        if slowdown == 0 or self._target is None:
            frame = next_frame()
            self._start = self._target = frame.astype(np.float32)
            self._phase = 1.0
            return frame
        self._phase += 1.0 - slowdown / 100.0
        if self._phase > 1.0 + 1e-9:
            self._start = self._target
            self._target = next_frame().astype(np.float32)
            self._phase -= 1.0
        amount = min(self._phase, 1.0)
        frame = self._start * (1.0 - amount) + self._target * amount
        return np.rint(frame).clip(0, 255).astype(np.uint8)


class AudioVisualizer:
    def __init__(
        self, style: str, sensitivity: float = DEFAULT_SENSITIVITY,
        controls: PaletteControls | None = None, slowdown: float = DEFAULT_SLOWDOWN,
        *, size: int = GRID_SIZE,
    ) -> None:
        if style not in {"spectrum", "wave"}:
            raise ValueError("style must be 'spectrum' or 'wave'")
        if not np.isfinite(sensitivity) or sensitivity <= 0:
            raise ValueError("sensitivity must be positive")
        if size not in (16, 32, 48):
            raise ValueError("display size must be 16, 32 or 48")
        self.size = size
        validate_slowdown(slowdown)
        self.slowdown = slowdown
        self._delayer = FrameDelayer()
        self.style = style
        self.sensitivity = sensitivity
        self.palette = np.full((self.size, 3), 255, dtype=np.uint8)
        self.controls = controls
        self._palette_revision = -1
        self._settings = default_settings() | {"slowdown": slowdown}
        self._moving_rainbow = False
        self._adaptive = self._settings["preset"] == "adaptive"
        if not self._adaptive:
            self.palette = make_palette(self._settings, size=self.size)
        self._energy = 0.0
        self._colorful = 0.0
        self._energy_history: deque[tuple[float, float, float]] = deque()
        self._energy_clock = 0.0
        self._song_levels: np.ndarray | None = None
        self._rainbow_phase = 0.0
        self._last_color_time: float | None = None
        self._output_brightness: BrightnessEnvelope | None = None
        self.levels = np.zeros(self.size, dtype=np.float32)
        self.previous = np.zeros((self.size, self.size, 3), dtype=np.float32)
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        frequencies = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
        self._energy_masks = [
            (frequencies >= low) & (frequencies < high)
            for low, high in ((45, 250), (250, 2500), (2500, 16000))
        ]
        edges = np.geomspace(45.0, 12_000.0, self.size + 1)
        self.band_masks = [
            (frequencies >= edges[index]) & (frequencies < edges[index + 1])
            for index in range(self.size)
        ]
        # At higher resolutions the narrow bass bands can be smaller than an FFT bin.
        # Share the nearest bin in those bands instead of leaving dead columns.
        for index, mask in enumerate(self.band_masks):
            if not np.any(mask):
                center = np.sqrt(edges[index] * edges[index + 1])
                mask[np.argmin(np.abs(frequencies - center))] = True
        self.volume_level = 0.0
        self._wave_phase = 0.0
        self.band_brightness = np.zeros(self.size, dtype=np.float32)

    def _update_volume_level(self, signal: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))
        target = amplitude_to_level(rms, self.sensitivity)
        rate = 0.72 if target > self.volume_level else 0.16
        self.volume_level += (target - self.volume_level) * rate
        return self.volume_level

    def _volume_brightness(self) -> float:
        # Keep quiet audio visible, but reserve the strong glow for loud passages.
        if self.volume_level <= 0.0:
            return 0.0
        return 0.06 + 0.94 * self.volume_level**1.35

    def render(self, samples: np.ndarray) -> np.ndarray:
        now = time.monotonic()
        elapsed = 0.0 if self._last_color_time is None else max(0.0, now - self._last_color_time)
        self._last_color_time = now
        if self.controls is not None:
            revision, settings = self.controls.settings_snapshot()
            if revision != self._palette_revision:
                self._palette_revision = revision
                self.slowdown = settings["slowdown"]
                changed = any(settings[key] != self._settings[key] for key in settings if key != "slowdown")
                if settings["preset"] != self._settings["preset"]:
                    self._rainbow_phase = 0.0
                    self._energy = 0.0
                    self._colorful = 0.0
                    self._energy_history.clear()
                    self._energy_clock = 0.0
                    self._song_levels = None
                    elapsed = 0.0
                self._settings = settings
                if changed:
                    self._moving_rainbow = settings["preset"] == "moving-rainbow"
                    self._adaptive = settings["preset"] == "adaptive"
                    # Animate a neutral brightness mask, then color it at the
                    # output cadence so even heavily slowed frames keep flowing.
                    self.palette = (np.full((self.size, 3), 255, dtype=np.uint8)
                                    if self._moving_rainbow or self._adaptive else make_palette(settings, size=self.size))
                    # Color edits apply immediately, even mid-transition.
                    self.previous.fill(0)
                    self._delayer.reset()
        frame = self._delayer.render(lambda: self._render_frame(samples), self.slowdown)
        display_palette = self.palette
        if self._moving_rainbow:
            self._rainbow_phase = (
                self._rainbow_phase + elapsed * (1.0 - self.slowdown / 100.0) / RAINBOW_CYCLE_SECONDS
            ) % 1.0
            display_palette = make_palette(self._settings, size=self.size, phase=self._rainbow_phase)
        elif self._adaptive:
            display_palette = self._adaptive_palette(samples, elapsed)
        if self._moving_rainbow or self._adaptive:
            frame = np.rint(frame.astype(np.float32) * display_palette[None, :, :] / 255.0).astype(np.uint8)
        if self._output_brightness is not None:
            frame = self._output_brightness.apply(frame, now)
        if self.controls is not None:
            self.controls.publish_frame(frame, display_palette)
        return frame

    def _adaptive_palette(self, samples: np.ndarray, elapsed: float) -> np.ndarray:
        self._energy_clock += max(0.0, elapsed)
        while self._energy_history and self._energy_history[0][0] < self._energy_clock - 30.0:
            self._energy_history.popleft()
        signal = np.zeros(FFT_SIZE, dtype=np.float64)
        tail = samples[-FFT_SIZE:]
        if len(tail):
            signal[-len(tail):] = tail
        signal -= signal.mean()
        # Integrated band power measures the energy of each region, including
        # energy spread across many notes. Normalize to RMS amplitude.
        power = np.abs(np.fft.rfft(signal * self.window)) ** 2
        energies = np.array([power[mask].sum() for mask in self._energy_masks])
        energies *= 2.0 / (FFT_SIZE * np.square(self.window).sum())
        amplitudes = np.sqrt(energies)
        total = float(energies.sum())
        levels = np.array([total, float(np.max(np.abs(signal))) ** 2]) * self.sensitivity**2
        # Measure sustained passages before remembering their maxima. A single
        # kick or click should not set the reference for the next half minute.
        if self._song_levels is None:
            self._song_levels = levels
        else:
            self._song_levels += (levels - self._song_levels) * (-np.expm1(-min(elapsed, 0.1) / 0.25))
        energy = colorful = 0.0
        if np.sqrt(total) * self.sensitivity >= 0.0005:
            # Music rarely has equal power in all three regions. Count a band
            # as present when it is within 24 dB of the strongest, fading that
            # contribution out by 42 dB down. Fullness adds color variety;
            # no single frequency region determines the palette temperature.
            relative_db = 20.0 * np.log10(max(float(amplitudes.min() / amplitudes.max()), 1e-12))
            presence = float(np.clip((relative_db + 42.0) / 18.0, 0, 1))
            presence = presence * presence * (3.0 - 2.0 * presence)
            rms_db, peak_db = 10.0 * np.log10(np.maximum(self._song_levels, 1e-12))
            self._energy_history.append((self._energy_clock, rms_db, peak_db))
            # Compare this passage with the song's recent maxima, not a fixed
            # dBFS curve that leaves most mastered music permanently warm.
            # RMS carries most of the weight: sparse hits reaching the same
            # peak as a dense chorus must still be able to turn Ocean.
            rms_max = max(-36.0, max(item[1] for item in self._energy_history))
            peak_max = max(-30.0, max(item[2] for item in self._energy_history))
            relative_db = 0.8 * (rms_db - rms_max) + 0.2 * (peak_db - peak_max)
            amount = float(np.clip((relative_db + 6.0) / 4.0, 0, 1))
            # A quiet room/noise floor cannot normalize itself into a chorus.
            audible = float(np.clip((rms_db + 60.0) / 18.0, 0, 1))
            energy = amount * amount * (3.0 - 2.0 * amount) * audible
            colorful = energy * (0.8 + 0.2 * presence)
        # Time-based envelopes prevent flashes on isolated beats and continue
        # cooling during silence. Slowdown stretches the transitions too.
        dt = min(elapsed, 0.1) / (1.0 + 3.0 * self.slowdown / 95.0)
        tau = 0.8 if energy > self._energy else 1.4
        self._energy += (energy - self._energy) * (-np.expm1(-dt / tau))
        self._colorful += (colorful - self._colorful) * (-np.expm1(-dt / tau))
        return make_adaptive_palette(self._settings, self._energy, self._colorful, size=self.size)

    def _render_frame(self, samples: np.ndarray) -> np.ndarray:
        if self.style == "spectrum":
            fresh = self._render_spectrum(samples)
        else:
            fresh = self._render_wave(samples)
        # Bright notes leave the panel sooner, while quiet details retain a
        # slightly longer phosphor-like trail. This applies to every band.
        frame_decay = 0.70 - 0.30 * self.volume_level
        self.previous *= frame_decay
        self.previous = np.maximum(self.previous, fresh.astype(np.float32))
        return np.rint(self.previous).clip(0, 255).astype(np.uint8)

    def _render_spectrum(self, samples: np.ndarray) -> np.ndarray:
        signal = samples[-FFT_SIZE:].astype(np.float32, copy=True)
        signal -= signal.mean()
        self._update_volume_level(signal)
        magnitudes = np.abs(np.fft.rfft(signal * self.window))
        magnitudes *= 2.0 / max(float(self.window.sum()), 1.0)
        bands = np.array(
            [float(np.max(magnitudes[mask])) if np.any(mask) else 0.0 for mask in self.band_masks],
            dtype=np.float32,
        )
        # Absolute dBFS levels preserve the difference between a quiet passage and
        # a loud one. The previous peak-normalization made both look equally big.
        targets = np.array(
            [amplitude_to_level(float(band), self.sensitivity) for band in bands],
            dtype=np.float32,
        )
        # Height reacts quickly, while brightness changes a little more gently.
        # Keeping separate envelopes avoids visible flashes between frames.
        level_release = np.clip(0.12 + 0.38 * self.levels, 0.12, 0.50)
        rates = np.where(targets > self.levels, 0.55, level_release)
        self.levels += (targets - self.levels) * rates
        brightness_release = np.clip(
            0.09 + 0.40 * self.band_brightness, 0.09, 0.49
        )
        brightness_rates = np.where(
            targets > self.band_brightness, 0.42, brightness_release
        )
        self.band_brightness += (
            targets - self.band_brightness
        ) * brightness_rates

        # Lightly blend neighboring envelopes so adjacent colors flow into one
        # another instead of changing brightness in isolated hard steps.
        padded_brightness = np.pad(self.band_brightness, 1, mode="edge")
        smooth_brightness = np.convolve(
            padded_brightness, np.array([0.05, 0.90, 0.05]), mode="valid"
        )

        # A faint audio-gated backdrop uses every LED, including columns with
        # little energy. Keep it high enough to survive RGB565 quantization.
        glow = (
            0.04 + 0.02 * self._volume_brightness()
            if np.max(np.abs(signal)) >= 0.0005 else 0.0
        )
        background = np.rint(self.palette * glow).astype(np.uint8)
        frame = np.broadcast_to(background, (self.size, self.size, 3)).copy()
        for column, level in enumerate(self.levels):
            height = min(self.size // 2, int(np.ceil(level * self.size / 2 * 1.25)))
            band_intensity = (
                0.0
                if smooth_brightness[column] < 0.005
                else 0.015 + 0.985 * smooth_brightness[column] ** 1.55
            )
            for offset in range(height):
                distance = offset / (self.size // 2 - 1)
                # Keep the center brightest without forcing the outer LEDs
                # to black when a loud band reaches the full panel height.
                axis_falloff = 0.18 + 0.82 * max(0.0, 1.0 - distance) ** 2.4
                brightness = band_intensity * axis_falloff
                color = np.rint(self.palette[column] * brightness).astype(np.uint8)
                color = np.maximum(color, background[column])
                frame[self.size // 2 - 1 - offset, column] = color
                frame[self.size // 2 + offset, column] = color
        return frame

    def _render_wave(self, samples: np.ndarray) -> np.ndarray:
        signal = samples[-FFT_SIZE:].astype(np.float32, copy=False)
        peak = float(np.max(np.abs(signal)))
        if peak < 0.0005:
            self._update_volume_level(signal)
            return np.zeros((self.size, self.size, 3), dtype=np.uint8)
        self._update_volume_level(signal)

        if self.slowdown > 0:
            return self._render_slow_wave()

        # Trigger on a rising zero crossing to keep musical waveforms steadier.
        crossings = np.flatnonzero((signal[:-1] <= 0) & (signal[1:] > 0))
        start = int(crossings[-1]) if len(crossings) else len(signal) // 2
        visible = signal[start : start + 720]
        if len(visible) < 32:
            visible = signal[-720:]
        positions = np.linspace(0, len(visible) - 1, self.size)
        values = np.interp(positions, np.arange(len(visible)), visible)
        wave_size = self.volume_level**1.25
        values = np.clip(values / peak * wave_size, -1.0, 1.0)
        center = (self.size - 1) / 2
        rows = np.rint(center - values * center).astype(int)

        frame = np.zeros((self.size, self.size, 3), dtype=np.uint8)
        color_scale = self._volume_brightness()
        for column in range(self.size):
            row = rows[column]
            color = np.rint(self.palette[column] * color_scale).astype(np.uint8)
            frame[row, column] = color
            if column == 0:
                continue
            previous_row = rows[column - 1]
            low, high = sorted((previous_row, row))
            for joined_row in range(low, high + 1):
                frame[joined_row, column] = color
        return frame

    def _render_slow_wave(self) -> np.ndarray:
        # One broad cycle spans the screen, even for noisy audio.
        # FrameDelayer controls the pace; applying slowdown here too would
        # multiply the slowdown and make the highest settings nearly freeze.
        angles = np.linspace(0.0, 2.0 * np.pi, self.size) - self._wave_phase
        self._wave_phase = (self._wave_phase + 0.12) % (2.0 * np.pi)
        amplitude = (self.size / 2 - 1) * self.volume_level**1.25
        centers = (self.size - 1) / 2 - amplitude * np.array([np.sin(angles), np.cos(angles)])
        half_width = (0.8 + 0.7 * self.slowdown / 95.0) * self.size / 16
        rows = np.arange(self.size)[:, None]
        intensity = np.zeros((self.size, self.size), dtype=np.float32)
        for center, strength in zip(centers, (1.0, 0.72)):
            # A solid core and soft, subpixel edges keep thick curves smooth
            # as their crests travel between LED rows.
            stroke = np.clip(half_width + 0.5 - np.abs(rows - center), 0.0, 1.0)
            intensity = np.maximum(intensity, stroke * strength)
        return np.rint(
            intensity[:, :, None] * self.palette[None, :, :] * self._volume_brightness()
        ).clip(0, 255).astype(np.uint8)


class RefreshRequested(Exception):
    """Return control to the main thread so it can clean up before restarting."""


def iter_audio_frames(
    capture: AudioCapture, visualizer: AudioVisualizer,
    refresh_requested: threading.Event | None = None,
) -> Iterator[bytes]:
    visualizer._output_brightness = BrightnessEnvelope()
    while True:
        if refresh_requested is not None and refresh_requested.is_set():
            raise RefreshRequested()
        yield encode_rgb565(visualizer.render(capture.latest(FFT_SIZE)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Animate an ESP32 LED screen from Mac system audio"
    )
    add_display_argument(parser)
    parser.add_argument(
        "--style",
        choices=("wave", "spectrum"),
        default="wave",
        help="visual style (default: wave)",
    )
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=DEFAULT_SENSITIVITY,
        help=f"audio response multiplier (default: {DEFAULT_SENSITIVITY:g})",
    )
    parser.add_argument(
        "--port", default="auto", help="serial port (default: auto-detect)"
    )
    parser.add_argument(
        "--port-wait",
        type=float,
        default=DEFAULT_PORT_WAIT,
        help="seconds to wait for an auto-detected board (default: 30)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_STREAM_FPS,
        help="LED refresh rate (default: 20 for 32x32 at 2,000,000 baud)",
    )
    parser.add_argument(
        "--slowdown", type=float, default=DEFAULT_SLOWDOWN,
        help="reduce animation updates by 0–95 percent and blend intervening frames (default: 20)",
    )
    parser.add_argument(
        "--timeout", type=float, default=0.2, help="response timeout (default: 0.2)"
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="packet retries (default: 3)"
    )
    parser.add_argument(
        "--clear-on-exit", action="store_true", help="turn LEDs off on exit"
    )
    parser.add_argument(
        "--controls-port", type=int, default=8765,
        help="local palette control port; 0 picks a free port (default: 8765)",
    )
    parser.add_argument(
        "--no-controls", action="store_true", help="disable live palette controls",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="print the controls URL without opening a browser",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not np.isfinite(args.fps) or args.fps <= 0:
        raise SystemExit("error: --fps must be positive")
    if not np.isfinite(args.sensitivity) or args.sensitivity <= 0:
        raise SystemExit("error: --sensitivity must be positive")
    try:
        validate_slowdown(args.slowdown)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    if args.port_wait < 0:
        raise SystemExit("error: --port-wait cannot be negative")
    if args.timeout <= 0:
        raise SystemExit("error: --timeout must be positive")
    if args.retries < 0:
        raise SystemExit("error: --retries cannot be negative")

    if not 0 <= args.controls_port <= 65535:
        raise SystemExit("error: --controls-port must be between 0 and 65535")

    capture = AudioCapture()
    controls = None if args.no_controls else PaletteControls(slowdown=args.slowdown, size=args.display_size)
    control_server: PaletteServer | None = None
    connection: SerialConnection | None = None
    refresh_requested = threading.Event()
    try:
        print(
            "Requesting access to the Mac's system audio. If prompted, allow "
            "System Audio Recording Only (screen access is not required)...",
            file=sys.stderr,
        )
        capture.start()
        visualizer = AudioVisualizer(
            args.style, args.sensitivity, controls, args.slowdown, size=args.display_size,
        )
        source = FrameSource(
            fps=args.fps,
            iter_frames=lambda: iter_audio_frames(capture, visualizer, refresh_requested),
            size=visualizer.size,
        )
        port = resolve_port(args.port, wait_timeout=args.port_wait)
        print(f"Opening {port}...", file=sys.stderr)
        connection = open_serial(port, args.timeout, baudrate=args.baud)
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, source.fps, args.timeout, max(args.retries, 3), args.display_size)
        print(
            f"Listening to Mac system audio; streaming {args.style} at "
            f"{source.fps:g} FPS. Ctrl-C stops.",
            file=sys.stderr,
        )
        if controls is not None:
            control_server = PaletteServer(controls, args.controls_port, on_refresh=refresh_requested.set)
            control_server.start()
            print(f"Live palette controls: {control_server.url}", file=sys.stderr)
            if not args.no_browser:
                try:
                    webbrowser.open(control_server.url)
                except webbrowser.Error:
                    print("Open the controls URL in your browser.", file=sys.stderr)
        stream_frames(
            connection,
            source,
            loop=False,
            timeout=args.timeout,
            retries=args.retries,
            drop_late=True,
            display_size=args.display_size,
        )
        return 0
    except RefreshRequested:
        print("Refreshing the app...", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if control_server is not None:
            control_server.close()
        capture.close()
        if connection is not None:
            if args.clear_on_exit:
                try:
                    exchange_packet(
                        connection,
                        PACKET_CLEAR,
                        0xFFFFFFFF,
                        b"",
                        STATUS_ACK,
                        args.timeout,
                        0,
                    )
                except (OSError, RuntimeError):
                    pass
            connection.close()

    # The old HTTP server, audio tap and serial connection are all closed.
    # Keep the actual controls port (including --controls-port 0) so the
    # existing browser tab reconnects; reload Python code without opening a tab.
    assert control_server is not None
    args.controls_port = control_server.port
    args.no_browser = True
    restart_args = []
    for name, value in vars(args).items():
        flag = "--" + name.replace("_", "-")
        if isinstance(value, bool):
            if value:
                restart_args.append(flag)
        else:
            restart_args.extend([flag, str(value)])
    try:
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *restart_args])
    except OSError as exc:
        print(f"error: could not restart the app: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
