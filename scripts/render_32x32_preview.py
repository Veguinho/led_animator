#!/usr/bin/env python3
"""Render the deterministic 32×32 spectrum image used by the documentation."""

from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import led_animator
from audio_palette_controls import PaletteControls, default_settings
from system_audio_visualizer import AudioVisualizer, FFT_SIZE, SAMPLE_RATE


def main() -> None:
    controls = PaletteControls(size=32, slowdown=0)
    controls.update(default_settings() | {
        "preset": "sunset", "brightness": 0.72, "slowdown": 0,
    })
    visualizer = AudioVisualizer("spectrum", controls=controls, slowdown=0, size=32)

    time = np.arange(FFT_SIZE, dtype=np.float32) / SAMPLE_RATE
    frequencies = (65.4, 130.8, 261.6, 523.3, 1046.5, 2093.0, 4186.0)
    signal = sum(
        (0.22 / (1 + index * 0.18)) * np.sin(2 * np.pi * frequency * time + index)
        for index, frequency in enumerate(frequencies)
    ).astype(np.float32)
    signal = np.clip(signal, -0.95, 0.95)
    for _ in range(18):
        frame = visualizer.render(signal)

    destination = ROOT / "docs" / "assets" / "spectrum-32x32.png"
    previous_grid_size = led_animator.GRID_SIZE
    try:
        led_animator.GRID_SIZE = 32
        image = led_animator.render_led_frame(frame, led_size=9, gap=2)
        image.save(destination, optimize=True)
        image.close()
    finally:
        led_animator.GRID_SIZE = previous_grid_size
    print(f"Rendered {destination.relative_to(ROOT)} ({frame.shape[1]}×{frame.shape[0]} LEDs)")


if __name__ == "__main__":
    main()
