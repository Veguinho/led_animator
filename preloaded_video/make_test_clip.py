#!/usr/bin/env python3
"""Generate a native 32x32 RGB/panel test; run it with main.py video."""

import argparse
from pathlib import Path
import subprocess

import numpy as np

FPS = 30


def fade_frames():
    """Twelve-second loop with constant total RGB intensity and no white."""
    colors = np.array(((180, 0, 0), (0, 180, 0), (0, 0, 180)), dtype=np.float32)
    for index, color in enumerate(colors):
        following = colors[(index + 1) % len(colors)]
        for frame in range(4 * FPS):
            blend = 0.5 - 0.5 * np.cos(np.pi * frame / (4 * FPS))
            rgb = np.rint(color * (1 - blend) + following * blend).astype(np.uint8)
            yield np.full((32, 32, 3), rgb, dtype=np.uint8)


def test_frames():
    # Two seconds per full-screen color, then one second per panel.
    # Crossfades and the uploader's dimming keep changes gentle.
    scenes = []
    for color in ((180, 0, 0), (0, 180, 0), (0, 0, 180), (180, 180, 180)):
        scenes.append((np.full((32, 32, 3), color, dtype=np.uint8), 2))
    for panel in range(4):
        frame = np.full((32, 32, 3), 80, dtype=np.uint8)
        row, column = divmod(panel, 2)
        frame[row * 16:(row + 1) * 16, column * 16:(column + 1) * 16] = 210
        scenes.append((frame, 1))
    previous = scenes[-1][0].astype(np.float32)
    for target, seconds in scenes:
        for index in range(seconds * FPS):
            blend = min(1.0, (index + 1) / (FPS * 0.4))
            blend = blend * blend * (3 - 2 * blend)
            yield np.rint(previous * (1 - blend) + target * blend).astype(np.uint8)
        previous = target.astype(np.float32)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--pattern", choices=("test", "fade"), default="test")
    args = parser.parse_args()
    if args.output is None:
        name = "color_fade_32x32.mp4" if args.pattern == "fade" else "panel_test_32x32.mp4"
        args.output = Path("video_clips") / name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames = np.stack(list(fade_frames() if args.pattern == "fade" else test_frames()))
    assert frames.shape[1:] == (32, 32, 3)
    assert np.all(frames.max(axis=0) >= 180)
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo",
        "-pixel_format", "rgb24", "-video_size", "32x32", "-framerate", str(FPS),
        "-i", "pipe:0", "-an", "-c:v", "libx264rgb", "-crf", "0",
        "-movflags", "+faststart", str(args.output),
    ], input=frames.tobytes(), check=True)
    print(f"Created {args.output}: 32x32, 30 FPS, {len(frames) / FPS:g} seconds, all 1024 pixels covered.")


if __name__ == "__main__":
    main()
