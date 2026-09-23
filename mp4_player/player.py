#!/usr/bin/env python3
"""Convert an MP4 to 48x48, buffer it in ESP32 PSRAM, and play at 30 FPS."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

# Also support `python mp4_player/player.py ...`.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from device_modes import flash_firmware, stop_mode_workers
from export_arduino import encode_rgb565
from led_animator import frame_to_led_grid, map_led_intensity, probe_video
from preloaded_video.player import (
    BAUD_RATES,
    DEFAULT_BAUD,
    FRAME_BYTES,
    MAX_CLIP_BYTES,
    STATUS,
    Clip,
    connect_video,
    exchange,
    preload,
    resolve_port,
    soften_frames,
)

DISPLAY_SIZE = 48
DEFAULT_FPS = 30
# Preserve enough source detail for the existing area-blending/downsampling
# algorithm without piping full 4K frames through Python.
MAX_BLEND_SIZE = DISPLAY_SIZE * 2


def _working_size(width: int, height: int) -> int:
    """Choose a bounded square size for blending into the LED grid."""
    return max(DISPLAY_SIZE, min(MAX_BLEND_SIZE, width, height))


def prepare_clip(
    path: Path,
    fps: int = DEFAULT_FPS,
    seconds: float | None = None,
    start: float = 0,
    gamma: float = 2.2,
    brightness: float = 0.25,
    smooth_ms: float = 180,
) -> Clip:
    """Decode, blend to 48x48, soften, and encode an MP4 for board memory."""
    if not path.is_file():
        raise ValueError(f"video does not exist: {path}")
    if not 1 <= fps <= 60:
        raise ValueError("FPS must be between 1 and 60")
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("seconds must be positive")
    if not math.isfinite(start) or start < 0 or not math.isfinite(gamma) or gamma <= 0:
        raise ValueError("start must be non-negative and gamma must be positive")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to prepare video")

    info = probe_video(path)
    side = min(info.width, info.height)
    working_size = _working_size(info.width, info.height)
    scaling = "area" if working_size < side else "bilinear"
    filters = (
        f"crop=min(iw\\,ih):min(iw\\,ih),"
        f"scale={working_size}:{working_size}:flags={scaling},fps={fps}"
    )
    command = [ffmpeg, "-v", "error", "-nostdin", "-ss", str(start), "-i", str(path)]
    if seconds is not None:
        command += ["-t", str(seconds)]
    command += [
        "-map", "0:v:0", "-an", "-vf", filters,
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]

    frame_size = working_size * working_size * 3
    frames: list[np.ndarray] = []
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        assert process.stdout is not None
        try:
            while True:
                raw = process.stdout.read(frame_size)
                if not raw:
                    break
                if len(raw) != frame_size:
                    raise RuntimeError("FFmpeg returned a truncated frame")
                if (len(frames) + 1) * FRAME_BYTES > MAX_CLIP_BYTES:
                    maximum = (MAX_CLIP_BYTES // FRAME_BYTES) / fps
                    raise ValueError(
                        f"clip is too long; use --seconds below {maximum:.2f} at {fps} FPS"
                    )
                rgb = np.frombuffer(raw, dtype=np.uint8).reshape(
                    working_size, working_size, 3
                )
                # This is the same neighboring-pixel/area blend used by the
                # offline LED converter, now applied automatically to MP4s.
                grid = frame_to_led_grid(rgb, DISPLAY_SIZE)
                frames.append(map_led_intensity(grid, gamma))
            code = process.wait()
            if code:
                errors.seek(0)
                raise RuntimeError(errors.read().decode(errors="replace").strip())
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    if not frames:
        raise ValueError("video selection contains no frames")
    softened = soften_frames(np.asarray(frames), fps, brightness, smooth_ms)
    return Clip(b"".join(encode_rgb565(frame) for frame in softened), fps)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="MP4 file to convert and buffer")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS,
                        choices=range(1, 61), metavar="1..60")
    parser.add_argument("--seconds", type=float,
                        help="clip duration; omit to use the whole file if it fits")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--led-gamma", type=float, default=2.2)
    parser.add_argument("--brightness", type=float, default=0.25,
                        help="pixel intensity multiplier, 0..1 (default: 0.25)")
    parser.add_argument("--smooth-ms", type=float, default=180,
                        help="temporal smoothing in milliseconds; 0 disables")
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, choices=BAUD_RATES, default=DEFAULT_BAUD)
    parser.add_argument("--no-upload", action="store_true",
                        help="MP4 player firmware is already installed")
    parser.add_argument("--prepare-only", action="store_true",
                        help="validate conversion and capacity without touching the board")
    parser.add_argument("--once", action="store_true",
                        help="play once and hold the last frame")
    parser.add_argument("--detach", action="store_true",
                        help="leave buffered playback running on the board")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    connection = None
    sequence = 1
    try:
        clip = prepare_clip(
            args.video.expanduser(), args.fps, args.seconds, args.start,
            args.led_gamma, args.brightness, args.smooth_ms,
        )
        print(
            f"Prepared {clip.frames} blended 48x48 frames at {clip.fps} FPS "
            f"({clip.frames / clip.fps:.2f} s), {len(clip.data):,} bytes. "
            f"USB preload takes at least {len(clip.data) / (args.baud / 10):.0f} seconds."
        )
        if args.prepare_only:
            return 0
        port = resolve_port(args.port, wait_timeout=30)
        if args.no_upload:
            stop_mode_workers()
        else:
            flash_firmware("video", port)
        connection = connect_video(port, args.baud)
        print(f"MP4 player connection: {args.baud:,} baud", flush=True)
        upload_start = time.monotonic()
        sequence = preload(connection, clip, loop=not args.once)
        upload_seconds = time.monotonic() - upload_start
        print(
            f"Buffered in {upload_seconds:.1f} s "
            f"({len(clip.data) / upload_seconds:,.0f} bytes/s)", flush=True,
        )
        print(
            f"Playing from board memory at {clip.fps} FPS. "
            "The buffer is lost on reset or power loss.", flush=True,
        )
        if args.detach:
            return 0
        print("Ctrl-C stops playback. Use --detach to leave it running.")
        previous = None
        while True:
            time.sleep(2)
            playing, displayed, missed = exchange(connection, STATUS, sequence)
            sequence += 1
            now = time.monotonic()
            if previous is not None:
                actual_fps = (displayed - previous[1]) / (now - previous[0])
                print(
                    f"Playback: {actual_fps:.1f} FPS; "
                    f"{missed} skipped deadlines total", flush=True,
                )
            previous = now, displayed
            if not playing:
                return 0
    except KeyboardInterrupt:
        if connection is not None:
            try:
                from preloaded_video.player import STOP
                exchange(connection, STOP, sequence + 1)
            except (OSError, RuntimeError):
                pass
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
