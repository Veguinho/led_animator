#!/usr/bin/env python3
"""Generate a seamless bouncing-ball animation on a black 16x16 LED grid."""

from __future__ import annotations

import argparse
import math
import tempfile
from pathlib import Path

import numpy as np

from led_animator import LedAnimation, save_preview_mp4


GRID_SIZE = 16
DEFAULT_FPS = 60.0
DEFAULT_DURATION = 6.0
TRAIL_LENGTH = 30


def bounce_position(progress: float) -> tuple[float, float]:
    """Trace a closed Lissajous-like path that visibly rebounds at every edge."""
    def triangle(value: float) -> float:
        phase = value % 1.0
        return 1.0 - abs(2.0 * phase - 1.0)

    margin = 1.0
    travel = GRID_SIZE - 1.0 - 2.0 * margin
    x = margin + travel * triangle(3.0 * progress + 0.07)
    y = margin + travel * triangle(2.0 * progress + 0.31)
    return x, y


def draw_bouncing_orb(frame: np.ndarray, frame_index: int, frame_count: int) -> None:
    """Draw a white ball followed by a red trail that fades with age."""
    ball_pixels = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))

    # Draw oldest samples first. Newer samples use the brighter red value when
    # several samples land on the same LED because of the grid's low resolution.
    for age in range(TRAIL_LENGTH, 0, -1):
        trail_progress = (frame_index - age) / frame_count
        trail_x_position, trail_y_position = bounce_position(trail_progress)
        trail_x = round(trail_x_position)
        trail_y = round(trail_y_position)
        fade = 1.0 - age / (TRAIL_LENGTH + 1.0)
        intensity = round(220 * fade**2)
        for dx, dy in ball_pixels:
            current_red = int(frame[trail_y + dy, trail_x + dx, 0])
            frame[trail_y + dy, trail_x + dx] = [
                max(current_red, intensity),
                0,
                0,
            ]

    progress = frame_index / frame_count
    x_position, y_position = bounce_position(progress)
    x = round(x_position)
    y = round(y_position)
    for dx, dy in ball_pixels:
        frame[y + dy, x + dx] = [255, 255, 255]


def generate_frames(
    frame_count: int,
    seed: int = 2026,
) -> np.ndarray:
    if frame_count < 2:
        raise ValueError("frame_count must be at least 2")

    # Kept for CLI/API compatibility; the plain ball animation is not random.
    del seed

    frames = np.zeros((frame_count, GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
    for index, frame in enumerate(frames):
        draw_bouncing_orb(frame, index, frame_count)
    return frames


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a white bouncing ball with a fading red trail."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output/psychedelic_space_bounce_16x16.mp4"),
        help="output MP4 path",
    )
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument(
        "--force", action="store_true", help="replace an existing MP4"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not math.isfinite(args.fps) or args.fps <= 0:
        raise SystemExit("error: --fps must be a positive finite number")
    if not math.isfinite(args.duration) or args.duration <= 0:
        raise SystemExit("error: --duration must be a positive finite number")
    if args.output.suffix.lower() != ".mp4":
        raise SystemExit("error: --output must end in .mp4")
    if args.output.exists() and not args.force:
        raise SystemExit(
            f"error: output already exists: {args.output} (use --force to replace)"
        )

    frame_count = max(2, round(args.duration * args.fps))
    animation = LedAnimation(generate_frames(frame_count, args.seed), args.fps)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f".{args.output.stem}.work-", dir=args.output.parent
    ) as directory:
        staged = Path(directory) / args.output.name
        save_preview_mp4(
            animation,
            staged,
            # One encoded pixel per LED: the MP4 itself is exactly 16x16.
            led_size=1,
            gap=0,
            preview_fps=args.fps,
            unlit_color=(0, 0, 0),
            crf=0,
            preserve_rgb=True,
        )
        staged.replace(args.output)

    print(
        f"Generated {frame_count} frames at {args.fps:.3f} FPS "
        f"({animation.duration:.2f} seconds) on a {GRID_SIZE}x{GRID_SIZE} RGB grid."
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
