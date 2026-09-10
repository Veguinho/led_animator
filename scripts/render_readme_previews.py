#!/usr/bin/env python3
"""Render lava and a spectrum preview driven by an actual audio recording."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lava_lamp_stream import LavaLampFluid
from led_animator import LedAnimation, save_preview_mp4
from system_audio_visualizer import AudioVisualizer, FFT_SIZE, SAMPLE_RATE

FPS = 30
SECONDS = 16
DESTINATION = ROOT / "docs" / "assets"


def spectrum_frames(samples: np.ndarray) -> list[np.ndarray]:
    """Use the same trailing FFT window, rainbow, and defaults as the board."""
    visualizer = AudioVisualizer("spectrum")
    frames = []
    for index in range(FPS * SECONDS):
        # Frame zero corresponds to the beginning of the recorded soundtrack.
        end = round(index * SAMPLE_RATE / FPS)
        window = np.zeros(FFT_SIZE, dtype=np.float32)
        available = samples[max(0, end - FFT_SIZE):end]
        if len(available):
            window[-len(available):] = available
        frames.append(visualizer.render(window))
    return frames


def export(name: str, frames: list[np.ndarray], audio: Path | None = None) -> None:
    video = DESTINATION / f"{name}.mp4"
    save_preview_mp4(
        LedAnimation(np.stack(frames), FPS), video,
        led_size=14, gap=2, preview_fps=FPS,
    )
    if audio is not None:
        with tempfile.TemporaryDirectory() as temporary:
            muxed = Path(temporary) / "with-audio.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-i", str(video),
                "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-t", str(SECONDS), "-movflags", "+faststart", str(muxed),
            ], check=True)
            video.write_bytes(muxed.read_bytes())
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-file", type=Path, help="real recorded audio (at least 16 seconds)")
    parser.add_argument("--lava-only", action="store_true", help="rebuild only the lava preview")
    parser.add_argument("--audio-only", action="store_true", help="rebuild only the spectrum preview")
    args = parser.parse_args()
    if args.lava_only and args.audio_only:
        parser.error("choose either --lava-only or --audio-only")
    if not args.lava_only and args.audio_file is None:
        parser.error("provide --audio-file with real recorded audio, or use --lava-only")
    samples = None
    if not args.lava_only:
        decoded = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(args.audio_file),
            "-t", str(SECONDS), "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
        ], check=True, capture_output=True)
        samples = np.frombuffer(decoded.stdout, dtype="<f4")
        if len(samples) < SAMPLE_RATE * SECONDS:
            parser.error("the audio recording must be at least 16 seconds long")
        if not np.all(np.isfinite(samples)) or np.max(np.abs(samples)) < 1e-5:
            parser.error("the audio recording contains no usable signal")
    DESTINATION.mkdir(parents=True, exist_ok=True)
    if not args.audio_only:
        lava = LavaLampFluid(fps=FPS, seed=2026, warmup_seconds=15)
        export("lava-lamp", [lava.render() for _ in range(FPS * SECONDS)])
    if samples is not None:
        export("audio-spectrum", spectrum_frames(samples), args.audio_file)


if __name__ == "__main__":
    main()
