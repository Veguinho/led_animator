#!/usr/bin/env python3
"""Render the README demos from the live renderers, without audio or hardware."""

from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lava_lamp_stream import LavaLampFluid
from led_animator import LedAnimation, save_preview_mp4
from system_audio_visualizer import AudioVisualizer, FFT_SIZE, SAMPLE_RATE

FPS = 30
SECONDS = 16
DESTINATION = ROOT / "docs" / "assets"


def demo_samples(frame_number: int) -> np.ndarray:
    """Synthetic changing tones with a quiet/loud envelope; no recorded music."""
    end = round((frame_number + 1) * SAMPLE_RATE / FPS)
    time = np.arange(end - FFT_SIZE, end, dtype=np.float64) / SAMPLE_RATE
    phase = 2 * np.pi * (95 * time + 18 * np.sin(2 * np.pi * time / 8))
    signal = np.sin(phase) + 0.32 * np.sin(phase * 2 + time)
    signal += 0.18 * np.sin(phase * 3 - time * 0.7)
    envelope = 10 ** ((-48 + 44 * (0.5 - 0.5 * np.cos(2 * np.pi * time / 8))) / 20)
    return (signal * envelope / 1.5).astype(np.float32)


def export(name: str, frames: list[np.ndarray]) -> None:
    video = DESTINATION / f"{name}.mp4"
    save_preview_mp4(
        LedAnimation(np.stack(frames), FPS), video,
        led_size=14, gap=2, preview_fps=FPS,
    )
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-filter_complex",
            "fps=12,split[a][b];[a]palettegen=max_colors=64[p];"
            "[b][p]paletteuse=dither=none",
            "-loop", "0", str(DESTINATION / f"{name}.gif"),
        ],
        check=True,
    )
    print(f"Rendered {name}: {SECONDS}s MP4 + looping GIF", flush=True)


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    lava = LavaLampFluid(fps=FPS, seed=2026, warmup_seconds=15)
    export("lava-lamp", [lava.render() for _ in range(FPS * SECONDS)])
    wave = AudioVisualizer("wave")
    export("audio-wave", [wave.render(demo_samples(i)) for i in range(FPS * SECONDS)])


if __name__ == "__main__":
    main()
