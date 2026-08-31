#!/usr/bin/env python3
"""Animate a 16x16 LED panel from the Mac's live system audio."""

from __future__ import annotations

import argparse
import colorsys
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from export_arduino import encode_rgb565
from stream_arduino import (
    DEFAULT_PORT_WAIT,
    DEFAULT_STREAM_FPS,
    PACKET_CLEAR,
    STATUS_ACK,
    FrameSource,
    SerialConnection,
    exchange_packet,
    handshake,
    open_serial,
    resolve_port,
    stream_frames,
)


ROOT = Path(__file__).resolve().parent
CAPTURE_SOURCE = ROOT / "macos_system_audio.swift"
CAPTURE_BINARY = ROOT / ".build" / "system_audio_capture"
SAMPLE_RATE = 48_000
GRID_SIZE = 16
FFT_SIZE = 2_048
MIN_DBFS = -72.0
MAX_DBFS = -6.0
DEFAULT_SENSITIVITY = 1.5


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
    if not CAPTURE_SOURCE.is_file():
        raise RuntimeError(f"capture helper source is missing: {CAPTURE_SOURCE}")
    if (
        CAPTURE_BINARY.is_file()
        and CAPTURE_BINARY.stat().st_mtime >= CAPTURE_SOURCE.stat().st_mtime
    ):
        return CAPTURE_BINARY

    CAPTURE_BINARY.parent.mkdir(parents=True, exist_ok=True)
    print("Building the macOS system-audio helper (first run only)...", file=sys.stderr)
    result = subprocess.run(
        ["xcrun", "swiftc", "-O", str(CAPTURE_SOURCE), "-o", str(CAPTURE_BINARY)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"could not build the system-audio helper: {detail}")
    return CAPTURE_BINARY


class AudioCapture:
    """Read mono Float32 samples from the native ScreenCaptureKit helper."""

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
                "timed out waiting for Screen & System Audio Recording permission"
            )
        message = self._process.stderr.readline().decode(errors="replace").strip()
        if message != "READY":
            self.close()
            detail = message.removeprefix("error: ") or "capture helper stopped"
            raise RuntimeError(
                f"could not capture Mac system audio: {detail}. In System Settings, "
                "allow your terminal under Privacy & Security > Screen & System "
                "Audio Recording, then run this command again"
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
    """Return one smooth dark-blue through purple to red X-axis gradient."""
    stops = (
        (0.00, (0.65, 0.93, 0.72)),  # dark blue
        (1.00, (0.00, 1.00, 1.00)),  # red
    )
    colors = []
    for column in range(GRID_SIZE):
        position = column / (GRID_SIZE - 1)
        for (left_position, left), (right_position, right) in zip(
            stops, stops[1:]
        ):
            if position <= right_position:
                amount = (position - left_position) / (
                    right_position - left_position
                )
                left_hue, left_saturation, left_value = left
                right_hue, right_saturation, right_value = right
                hue_delta = (right_hue - left_hue + 0.5) % 1.0 - 0.5
                hue = (left_hue + hue_delta * amount) % 1.0
                saturation = left_saturation + (
                    right_saturation - left_saturation
                ) * amount
                value = left_value + (right_value - left_value) * amount
                colors.append(colorsys.hsv_to_rgb(hue, saturation, value))
                break
    return np.rint(np.asarray(colors) * 255).clip(0, 255).astype(np.uint8)


class AudioVisualizer:
    def __init__(
        self, style: str, sensitivity: float = DEFAULT_SENSITIVITY
    ) -> None:
        if style not in {"spectrum", "wave"}:
            raise ValueError("style must be 'spectrum' or 'wave'")
        if not np.isfinite(sensitivity) or sensitivity <= 0:
            raise ValueError("sensitivity must be positive")
        self.style = style
        self.sensitivity = sensitivity
        self.palette = color_palette()
        self.levels = np.zeros(GRID_SIZE, dtype=np.float32)
        self.previous = np.zeros((GRID_SIZE, GRID_SIZE, 3), dtype=np.float32)
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        frequencies = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
        edges = np.geomspace(45.0, 12_000.0, GRID_SIZE + 1)
        self.band_masks = [
            (frequencies >= edges[index]) & (frequencies < edges[index + 1])
            for index in range(GRID_SIZE)
        ]
        self.volume_level = 0.0
        self.band_brightness = np.zeros(GRID_SIZE, dtype=np.float32)

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

        frame = np.zeros((GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
        for column, level in enumerate(self.levels):
            height = min(8, int(np.ceil(level * 8.0)))
            band_intensity = (
                0.0
                if smooth_brightness[column] <= 0.0
                else 0.015 + 0.985 * smooth_brightness[column] ** 1.55
            )
            for offset in range(height):
                distance = offset / (GRID_SIZE // 2 - 1)
                axis_falloff = max(0.0, 1.0 - distance) ** 2.4
                brightness = band_intensity * axis_falloff
                color = np.rint(self.palette[column] * brightness).astype(np.uint8)
                frame[7 - offset, column] = color
                frame[8 + offset, column] = color
        return frame

    def _render_wave(self, samples: np.ndarray) -> np.ndarray:
        signal = samples[-FFT_SIZE:].astype(np.float32, copy=False)
        peak = float(np.max(np.abs(signal)))
        if peak < 0.0005:
            self._update_volume_level(signal)
            return np.zeros((GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
        self._update_volume_level(signal)

        # Trigger on a rising zero crossing to keep musical waveforms steadier.
        crossings = np.flatnonzero((signal[:-1] <= 0) & (signal[1:] > 0))
        start = int(crossings[-1]) if len(crossings) else len(signal) // 2
        visible = signal[start : start + 720]
        if len(visible) < 32:
            visible = signal[-720:]
        positions = np.linspace(0, len(visible) - 1, GRID_SIZE)
        values = np.interp(positions, np.arange(len(visible)), visible)
        wave_size = self.volume_level**1.25
        values = np.clip(values / peak * wave_size, -1.0, 1.0)
        rows = np.rint(7.5 - values * 7.0).astype(int)

        frame = np.zeros((GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
        color_scale = self._volume_brightness()
        for column in range(GRID_SIZE):
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


def iter_audio_frames(
    capture: AudioCapture, visualizer: AudioVisualizer
) -> Iterator[bytes]:
    while True:
        yield encode_rgb565(visualizer.render(capture.latest(FFT_SIZE)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Animate a 16x16 ESP32 LED panel from Mac system audio"
    )
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
        help="LED refresh rate (default: 30)",
    )
    parser.add_argument(
        "--timeout", type=float, default=1.0, help="response timeout (default: 1)"
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="packet retries (default: 3)"
    )
    parser.add_argument(
        "--clear-on-exit", action="store_true", help="turn LEDs off on exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not np.isfinite(args.fps) or args.fps <= 0:
        raise SystemExit("error: --fps must be positive")
    if not np.isfinite(args.sensitivity) or args.sensitivity <= 0:
        raise SystemExit("error: --sensitivity must be positive")
    if args.port_wait < 0:
        raise SystemExit("error: --port-wait cannot be negative")
    if args.timeout <= 0:
        raise SystemExit("error: --timeout must be positive")
    if args.retries < 0:
        raise SystemExit("error: --retries cannot be negative")

    capture = AudioCapture()
    connection: SerialConnection | None = None
    try:
        print(
            "Requesting access to the Mac's system audio. If prompted, allow "
            "Screen & System Audio Recording...",
            file=sys.stderr,
        )
        capture.start()
        visualizer = AudioVisualizer(args.style, args.sensitivity)
        source = FrameSource(
            fps=args.fps,
            iter_frames=lambda: iter_audio_frames(capture, visualizer),
        )
        port = resolve_port(args.port, wait_timeout=args.port_wait)
        print(f"Opening {port}...", file=sys.stderr)
        connection = open_serial(port, args.timeout)
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, source.fps, args.timeout, max(args.retries, 3))
        print(
            f"Listening to Mac system audio; streaming {args.style} at "
            f"{source.fps:g} FPS. Ctrl-C stops.",
            file=sys.stderr,
        )
        stream_frames(
            connection,
            source,
            loop=False,
            timeout=args.timeout,
            retries=args.retries,
            drop_late=True,
        )
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
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


if __name__ == "__main__":
    raise SystemExit(main())
