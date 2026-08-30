#!/usr/bin/env python3
"""Generate a seamless psychedelic 16x16 space-bounce LED animation."""

from __future__ import annotations

import argparse
import colorsys
import math
import tempfile
from pathlib import Path

import numpy as np

from led_animator import LedAnimation, save_preview_mp4


GRID_SIZE = 16
DEFAULT_FPS = 20.0
DEFAULT_DURATION = 6.0


def rainbow(hue: float, brightness: float = 1.0) -> np.ndarray:
    """Return one saturated RGB color with an LED-friendly brightness curve."""
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 0.92, brightness)
    return np.rint(np.array([red, green, blue]) * 255.0).astype(np.int16)


def add_pixel(frame: np.ndarray, x: int, y: int, color: np.ndarray) -> None:
    """Add light to a pixel, clipping at the RGB channel maximum."""
    if 0 <= x < GRID_SIZE and 0 <= y < GRID_SIZE:
        frame[y, x] = np.minimum(
            255, frame[y, x].astype(np.int16) + color.astype(np.int16)
        ).astype(np.uint8)


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


def draw_background(frame: np.ndarray, progress: float) -> None:
    """Draw a dim rotating rainbow nebula that leaves headroom for highlights."""
    time_angle = math.tau * progress
    for y in range(GRID_SIZE):
        for x in range(GRID_SIZE):
            dx = x - 7.5
            dy = y - 7.5
            radius = math.hypot(dx, dy)
            angle = math.atan2(dy, dx)
            wave = (
                math.sin(radius * 1.55 - time_angle * 2.0)
                + math.sin(angle * 3.0 + time_angle * 3.0)
            ) * 0.5
            brightness = 0.018 + 0.025 * (wave + 1.0) * 0.5
            hue = angle / math.tau + radius * 0.045 + progress
            frame[y, x] = rainbow(hue, brightness).astype(np.uint8)


def draw_stars(
    frame: np.ndarray,
    progress: float,
    stars: list[tuple[int, int, float, float]],
) -> None:
    for x, y, phase, hue in stars:
        twinkle = max(0.0, math.sin(math.tau * (progress * 4.0 + phase)))
        color = rainbow(hue + progress * 0.15, 0.08 + 0.42 * twinkle**5)
        add_pixel(frame, x, y, color)


def draw_ship(
    frame: np.ndarray,
    left: int,
    top: int,
    hue: float,
    facing_right: bool,
) -> None:
    """Draw a tiny, readable five-by-four rocket with a white cockpit."""
    # 0=empty, 1=hull, 2=wing, 3=cockpit, 4=engine flame.
    sprite = np.array(
        [
            [0, 0, 2, 0, 0],
            [4, 1, 1, 3, 2],
            [4, 1, 1, 1, 2],
            [0, 0, 2, 0, 0],
        ],
        dtype=np.uint8,
    )
    if not facing_right:
        sprite = np.fliplr(sprite)

    palette = {
        1: rainbow(hue, 0.92),
        2: rainbow(hue + 0.17, 0.78),
        3: np.array([245, 255, 255], dtype=np.int16),
        4: rainbow(hue + 0.55, 1.0),
    }
    for row, values in enumerate(sprite):
        for column, value in enumerate(values):
            if value:
                add_pixel(frame, left + column, top + row, palette[int(value)])


def draw_spaceships(frame: np.ndarray, progress: float) -> None:
    # Each ship wraps while fully off-screen, keeping the animation seamless.
    route = progress * 25.0
    ship_one_x = math.floor(route) - 5
    ship_one_y = 2 + round(1.5 * math.sin(math.tau * progress * 2.0))
    draw_ship(frame, ship_one_x, ship_one_y, progress + 0.52, True)

    ship_two_x = 16 - math.floor((route + 12.5) % 25.0)
    ship_two_y = 10 + round(1.5 * math.sin(math.tau * progress * 3.0 + 1.4))
    draw_ship(frame, ship_two_x, ship_two_y, progress + 0.84, False)


def draw_bouncing_orb(frame: np.ndarray, frame_index: int, frame_count: int) -> None:
    """Draw a white-hot bouncing orb with a long, hue-shifting comet trail."""
    trail_length = 13
    for age in range(trail_length, 0, -1):
        trail_progress = (frame_index - age) / frame_count
        x, y = bounce_position(trail_progress)
        fade = (1.0 - age / (trail_length + 1.0)) ** 2
        color = rainbow(trail_progress * 2.6 + age * 0.018, 0.48 * fade)
        add_pixel(frame, round(x), round(y), color)

    progress = frame_index / frame_count
    x_position, y_position = bounce_position(progress)
    x = round(x_position)
    y = round(y_position)
    hue = progress * 2.6

    halo = rainbow(hue, 0.55)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        add_pixel(frame, x + dx, y + dy, halo)

    sparkle = 0.5 + 0.5 * math.sin(math.tau * progress * 12.0)
    if sparkle > 0.72:
        ray = rainbow(hue + 0.5, 0.30 * sparkle)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            add_pixel(frame, x + dx, y + dy, ray)

    frame[y, x] = [255, 255, 255]


def generate_frames(
    frame_count: int,
    seed: int = 2026,
) -> np.ndarray:
    if frame_count < 2:
        raise ValueError("frame_count must be at least 2")

    random = np.random.default_rng(seed)
    star_positions = random.choice(GRID_SIZE * GRID_SIZE, size=18, replace=False)
    stars = [
        (
            int(position % GRID_SIZE),
            int(position // GRID_SIZE),
            float(random.random()),
            float(random.random()),
        )
        for position in star_positions
    ]

    frames = np.zeros((frame_count, GRID_SIZE, GRID_SIZE, 3), dtype=np.uint8)
    for index, frame in enumerate(frames):
        progress = index / frame_count
        draw_background(frame, progress)
        draw_stars(frame, progress, stars)
        draw_spaceships(frame, progress)
        draw_bouncing_orb(frame, index, frame_count)
    return frames


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a psychedelic bouncing-orb and spaceship LED loop."
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
    parser.add_argument("--led-size", type=int, default=16)
    parser.add_argument("--gap", type=int, default=0)
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
            led_size=args.led_size,
            gap=args.gap,
            preview_fps=args.fps,
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
