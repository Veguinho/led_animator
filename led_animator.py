#!/usr/bin/env python3
"""Convert a video into a 16x16 RGB LED animation.

The only Python dependencies are NumPy and Pillow. FFmpeg and ffprobe are used
to decode input videos, which keeps the script independent of OpenCV.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
from numpy.lib import format as npy_format
from PIL import Image, ImageChops, ImageDraw, ImageFilter


GRID_SIZE = 16
FORMAT_VERSION = 1


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float


@dataclass
class LedAnimation:
    frames: np.ndarray
    fps: float

    def __post_init__(self) -> None:
        self.frames = np.asarray(self.frames, dtype=np.uint8)
        expected = (GRID_SIZE, GRID_SIZE, 3)
        if self.frames.ndim != 4 or self.frames.shape[1:] != expected:
            raise ValueError(
                f"frames must have shape (frame_count, {GRID_SIZE}, "
                f"{GRID_SIZE}, 3), got {self.frames.shape}"
            )
        if len(self.frames) == 0:
            raise ValueError("animation must contain at least one frame")
        if not math.isfinite(self.fps) or self.fps <= 0:
            raise ValueError("fps must be a positive finite number")

    @property
    def duration(self) -> float:
        return len(self.frames) / self.fps

    def close(self) -> None:
        """Release an underlying memory-mapped frame store, when present."""
        owner: object = self.frames
        while owner is not None:
            if isinstance(owner, np.memmap) and owner._mmap is not None:
                owner._mmap.close()
                return
            owner = getattr(owner, "base", None)


def _require_program(name: str) -> str:
    location = shutil.which(name)
    if location is None:
        raise RuntimeError(
            f"{name} was not found. Install FFmpeg and make sure {name} is on PATH."
        )
    return location


def _parse_rate(rate: str) -> float:
    numerator, separator, denominator = rate.partition("/")
    if separator:
        value = float(numerator) / float(denominator)
    else:
        value = float(rate)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"invalid video frame rate: {rate}")
    return value


def probe_video(path: Path) -> VideoInfo:
    ffprobe = _require_program("ffprobe")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown ffprobe error"
        raise RuntimeError(f"could not inspect {path}: {detail}")

    try:
        stream = json.loads(result.stdout)["streams"][0]
        return VideoInfo(
            width=int(stream["width"]),
            height=int(stream["height"]),
            fps=_parse_rate(stream["avg_frame_rate"]),
        )
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"{path} does not contain a readable video stream") from exc


def iter_square_video_frames(
    path: Path,
    info: VideoInfo,
    output_size: int | None = None,
) -> Iterator[np.ndarray]:
    """Decode center-cropped RGB frames, optionally scaled before piping."""
    ffmpeg = _require_program("ffmpeg")
    side = min(info.width, info.height)
    x = (info.width - side) // 2
    y = (info.height - side) // 2
    decoded_side = output_size or side
    filters = [f"crop={side}:{side}:{x}:{y}"]
    if output_size is not None and side != output_size:
        scaling = "area" if output_size < side else "bilinear"
        filters.append(f"scale={output_size}:{output_size}:flags={scaling}")
    filters.append("format=rgb24")
    frame_bytes = decoded_side * decoded_side * 3
    command = [
        ffmpeg,
        "-v",
        "error",
        "-noautorotate",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        ",".join(filters),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    assert process.stderr is not None

    try:
        while True:
            data = process.stdout.read(frame_bytes)
            if not data:
                break
            if len(data) != frame_bytes:
                raise RuntimeError("FFmpeg returned an incomplete video frame")
            yield np.frombuffer(data, dtype=np.uint8).reshape(
                decoded_side, decoded_side, 3
            )
    finally:
        process.stdout.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        return_code = process.wait()
        if return_code != 0 and sys.exc_info()[0] is None:
            raise RuntimeError(f"FFmpeg could not decode {path}: {stderr}")


def merge_neighboring_pixels(frame: np.ndarray) -> np.ndarray:
    """Merge each 2x2 neighborhood by averaging its RGB intensities."""
    if frame.ndim != 3 or frame.shape[0] != frame.shape[1] or frame.shape[2] != 3:
        raise ValueError("frame must be a square RGB image")
    if frame.shape[0] % 2:
        raise ValueError("frame side must be even when merging 2x2 neighborhoods")

    # uint16 prevents overflow while summing four uint8 channels. Adding two
    # implements round-to-nearest instead of always rounding down.
    values = frame.astype(np.uint16)
    merged = (
        values[0::2, 0::2]
        + values[0::2, 1::2]
        + values[1::2, 0::2]
        + values[1::2, 1::2]
        + 2
    ) // 4
    return merged.astype(np.uint8)


def _nearest_halving_size(side: int, target: int) -> int:
    """Return target * 2**n nearest to side, never below target."""
    if side <= target:
        return target
    exponent = max(0, round(math.log2(side / target)))
    return target * (2**exponent)


def frame_to_led_grid(frame: np.ndarray, grid_size: int = GRID_SIZE) -> np.ndarray:
    """Normalize a square frame, then repeatedly merge 2x2 neighborhoods."""
    if frame.ndim != 3 or frame.shape[0] != frame.shape[1] or frame.shape[2] != 3:
        raise ValueError("frame must be a square RGB image")
    if grid_size <= 0:
        raise ValueError("grid_size must be positive")

    side = frame.shape[0]
    if grid_size & (grid_size - 1):
        method = Image.Resampling.BOX if grid_size < side else Image.Resampling.BILINEAR
        return np.asarray(
            Image.fromarray(frame, mode="RGB").resize(
                (grid_size, grid_size), resample=method
            )
        ).astype(np.uint8, copy=False)

    normalized_side = _nearest_halving_size(side, grid_size)
    if side != normalized_side:
        # BOX merges source areas when shrinking; bilinear interpolation avoids
        # block replication when a small normalization upscale is needed.
        method = Image.Resampling.BOX if normalized_side < side else Image.Resampling.BILINEAR
        normalized = np.asarray(
            Image.fromarray(frame, mode="RGB").resize(
                (normalized_side, normalized_side), resample=method
            )
        )
    else:
        normalized = frame

    while normalized.shape[0] > grid_size:
        normalized = merge_neighboring_pixels(normalized)
    return normalized.astype(np.uint8, copy=False)


def generate_animation(
    video_path: Path,
    frame_store_path: Path | None = None,
) -> LedAnimation:
    """Convert frames one at a time, optionally writing each directly to disk."""
    info = probe_video(video_path)
    expected_shape = (GRID_SIZE, GRID_SIZE, 3)
    frame_count = 0

    if frame_store_path is None:
        frames: list[np.ndarray] = []
        output = None
    else:
        frame_store_path.parent.mkdir(parents=True, exist_ok=True)
        frames = []
        output = frame_store_path.open("wb")

    try:
        decoded_frames = iter_square_video_frames(video_path, info, GRID_SIZE)
        for frame_count, frame in enumerate(decoded_frames, start=1):
            grid = (
                frame
                if frame.shape == expected_shape
                else frame_to_led_grid(frame, GRID_SIZE)
            )
            if output is None:
                frames.append(grid)
            else:
                output.write(memoryview(np.ascontiguousarray(grid)).cast("B"))
            del grid, frame
            if frame_count % 100 == 0:
                print(f"Processed {frame_count} frames...", file=sys.stderr)
    finally:
        if output is not None:
            output.close()

    if frame_count == 0:
        raise RuntimeError(f"no frames were decoded from {video_path}")

    if frame_store_path is None:
        frame_array = np.stack(frames)
    else:
        frame_array = np.memmap(
            frame_store_path,
            dtype=np.uint8,
            mode="r",
            shape=(frame_count, GRID_SIZE, GRID_SIZE, 3),
        )
    return LedAnimation(frame_array, info.fps)


def _write_npz_member(
    archive: zipfile.ZipFile,
    name: str,
    value: np.ndarray,
    batch_size: int,
) -> None:
    array = np.asarray(value)
    with archive.open(f"{name}.npy", "w", force_zip64=True) as member:
        if name != "frames":
            npy_format.write_array(member, array, allow_pickle=False)
            return

        header = {
            "descr": npy_format.dtype_to_descr(array.dtype),
            "fortran_order": False,
            "shape": array.shape,
        }
        npy_format.write_array_header_2_0(member, header)
        for start in range(0, len(array), batch_size):
            chunk = np.ascontiguousarray(array[start : start + batch_size])
            member.write(memoryview(chunk).cast("B"))
            del chunk


def save_npz(animation: LedAnimation, path: Path, batch_size: int = 16) -> None:
    """Save a compact animation while compressing bounded frame batches."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    values = {
        "frames": animation.frames,
        "fps": np.asarray(animation.fps, dtype=np.float64),
        "grid_size": np.asarray(GRID_SIZE, dtype=np.int16),
        "color_order": np.array("RGB"),
        "pixel_order": np.array("row-major, top-left origin"),
        "format_version": np.asarray(FORMAT_VERSION, dtype=np.int16),
    }
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for name, value in values.items():
            _write_npz_member(archive, name, value, batch_size)


def load_npz(
    path: Path,
    frame_store_path: Path | None = None,
) -> LedAnimation:
    with np.load(path, allow_pickle=False) as data:
        fps = float(data["fps"])
        if frame_store_path is None:
            frames = data["frames"]
    if frame_store_path is not None:
        with zipfile.ZipFile(path) as archive:
            with (
                archive.open("frames.npy") as source,
                frame_store_path.open("wb") as destination,
            ):
                shutil.copyfileobj(source, destination, length=1024 * 1024)
        frames = np.load(frame_store_path, mmap_mode="r", allow_pickle=False)
    return LedAnimation(frames, fps)


def save_led_map(animation: LedAnimation, path: Path) -> None:
    """Stream human- and microcontroller-friendly RGB maps to JSON."""
    frame_duration_us = round(1_000_000 / animation.fps)
    metadata = {
        "format": "led-grid-animation",
        "version": FORMAT_VERSION,
        "width": GRID_SIZE,
        "height": GRID_SIZE,
        "color_order": "RGB",
        "channel_range": [0, 255],
        "pixel_order": "row-major, top-left origin",
        "fps": animation.fps,
        "frame_duration_us": frame_duration_us,
        "frame_count": len(animation.frames),
    }
    with path.open("w", encoding="utf-8") as output:
        encoded_metadata = json.dumps(metadata, separators=(",", ":"))
        output.write(encoded_metadata[:-1])
        output.write(',"frames":[')
        for frame_index, frame in enumerate(animation.frames):
            if frame_index:
                output.write(",")
            output.write("[")
            for row_index, row in enumerate(frame):
                if row_index:
                    output.write(",")
                json.dump(row.tolist(), output, separators=(",", ":"))
            output.write("]")
        output.write("]}\n")


def _led_cell_mask(led_size: int, gap: int, inset: int = 0) -> np.ndarray:
    """Return one pitch-sized mask matching Pillow's LED ellipse rasterization."""
    pitch = led_size + gap
    mask = Image.new("1", (pitch, pitch), 0)
    draw = ImageDraw.Draw(mask)
    right = led_size - 1 - inset
    if inset <= right:
        draw.ellipse((inset, inset, right, right), fill=1)
    return np.asarray(mask, dtype=bool)


def _expand_led_cells(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Expand one RGB value per LED into masked display cells."""
    expanded = values[:, :, None, None, :] * mask[None, None, :, :, None]
    return expanded.transpose(0, 2, 1, 3, 4).reshape(
        values.shape[0] * mask.shape[0],
        values.shape[1] * mask.shape[1],
        3,
    )


def _render_large_led_frame(
    frame: np.ndarray,
    led_size: int,
    gap: int,
) -> Image.Image:
    """Vectorized LED renderer for large grids."""
    pitch = led_size + gap
    canvas_size = gap + GRID_SIZE * pitch
    source = frame.astype(np.float32) / 255.0
    brightness = source.max(axis=2)
    boosted_float = np.power(source, 0.72) * 255.0
    boosted = np.rint(boosted_float).astype(np.uint8)

    face_mask = _led_cell_mask(led_size, gap)
    outer_values = np.rint(
        boosted_float * (0.10 + 0.28 * brightness[:, :, None])
    ).astype(np.uint8)
    inner_values = np.rint(
        boosted_float * (0.22 + 0.38 * brightness[:, :, None])
    ).astype(np.uint8)

    outer_glow = np.zeros((canvas_size, canvas_size, 3), dtype=np.uint8)
    inner_glow = np.zeros_like(outer_glow)
    outer_glow[gap:, gap:] = _expand_led_cells(outer_values, face_mask)
    inner_glow[gap:, gap:] = _expand_led_cells(inner_values, face_mask)
    del outer_values, inner_values

    outer_image = Image.fromarray(outer_glow, mode="RGB").filter(
        ImageFilter.GaussianBlur(radius=max(0.75, led_size * 0.55))
    )
    inner_image = Image.fromarray(inner_glow, mode="RGB").filter(
        ImageFilter.GaussianBlur(radius=max(0.45, led_size * 0.20))
    )
    image = ImageChops.add(outer_image, inner_image)
    outer_image.close()
    inner_image.close()
    del outer_glow, inner_glow

    pixels = np.asarray(image).copy()
    image.close()
    region = pixels[gap:, gap:]
    expanded_face_mask = np.tile(face_mask, (GRID_SIZE, GRID_SIZE))
    face_values = np.empty_like(boosted)
    face_values[:] = [4, 3, 2]
    lit = brightness > 0
    face_values[lit] = boosted[lit]
    expanded_faces = _expand_led_cells(face_values, face_mask)
    region[expanded_face_mask] = expanded_faces[expanded_face_mask]
    del expanded_faces, face_values

    inset = max(1, round(led_size * 0.27))
    highlight_mask = _led_cell_mask(led_size, gap, inset)
    if np.any(highlight_mask):
        white_mix = 0.62 * brightness[:, :, None] ** 2
        highlights = np.rint(
            boosted_float * (1.0 - white_mix) + 255.0 * white_mix
        ).astype(np.uint8)
        highlights[~lit] = 0
        expanded_highlights = _expand_led_cells(highlights, highlight_mask)
        expanded_highlight_mask = np.tile(
            highlight_mask, (GRID_SIZE, GRID_SIZE)
        )
        active_highlights = expanded_highlight_mask & np.repeat(
            np.repeat(lit, pitch, axis=0), pitch, axis=1
        )
        region[active_highlights] = expanded_highlights[active_highlights]

    return Image.fromarray(pixels, mode="RGB")


def render_led_frame(frame: np.ndarray, led_size: int = 20, gap: int = 2) -> Image.Image:
    """Render one grid as bright circular LEDs with camera-like color bloom."""
    if led_size < 1 or gap < 0:
        raise ValueError("led_size must be at least 1 and gap cannot be negative")

    # At one pixel per LED with no gaps, the final crisp LED faces cover the
    # bloom layers completely. Produce that exact result without constructing
    # thousands of Python tuples for a large-grid preview frame.
    if led_size == 1 and gap == 0:
        source = frame.astype(np.float32) / 255.0
        boosted = np.rint(np.power(source, 0.72) * 255.0).astype(np.uint8)
        rendered = np.empty_like(frame)
        rendered[:] = [4, 3, 2]
        lit = np.any(frame != 0, axis=2)
        rendered[lit] = boosted[lit]
        return Image.fromarray(rendered, mode="RGB")

    if GRID_SIZE >= 32:
        return _render_large_led_frame(frame, led_size, gap)

    pitch = led_size + gap
    canvas_size = gap + GRID_SIZE * pitch

    # Build two bloom layers before drawing the crisp LED faces. Blurring the
    # complete layer lets neighboring halos mix additively, as light does when
    # a camera records a bright LED panel.
    outer_glow = Image.new("RGB", (canvas_size, canvas_size), "black")
    inner_glow = Image.new("RGB", (canvas_size, canvas_size), "black")
    outer_draw = ImageDraw.Draw(outer_glow)
    inner_draw = ImageDraw.Draw(inner_glow)
    led_colors: list[tuple[tuple[int, int, int, int], tuple[int, int, int], float]] = []

    for row in range(GRID_SIZE):
        for column in range(GRID_SIZE):
            x = gap + column * pitch
            y = gap + row * pitch
            bounds = (x, y, x + led_size - 1, y + led_size - 1)
            source = frame[row, column].astype(np.float32) / 255.0

            # A camera makes illuminated LEDs look brighter than their raw
            # channel values. Gamma lift keeps the hue while revealing dim
            # colors; bloom remains tied to the LED's actual peak intensity.
            brightness = float(source.max())
            boosted = np.power(source, 0.72) * 255.0
            color = tuple(int(round(channel)) for channel in boosted)
            led_colors.append((bounds, color, brightness))

            if brightness > 0:
                outer_color = tuple(
                    int(round(channel * (0.10 + 0.28 * brightness)))
                    for channel in boosted
                )
                inner_color = tuple(
                    int(round(channel * (0.22 + 0.38 * brightness)))
                    for channel in boosted
                )
                outer_draw.ellipse(bounds, fill=outer_color)
                inner_draw.ellipse(bounds, fill=inner_color)

    outer_glow = outer_glow.filter(
        ImageFilter.GaussianBlur(radius=max(0.75, led_size * 0.55))
    )
    inner_glow = inner_glow.filter(
        ImageFilter.GaussianBlur(radius=max(0.45, led_size * 0.20))
    )
    image = ImageChops.add(outer_glow, inner_glow)
    draw = ImageDraw.Draw(image)

    for bounds, color, brightness in led_colors:
        # A barely visible face gives switched-off LEDs physical presence.
        draw.ellipse(bounds, fill=(4, 3, 2))
        if brightness == 0:
            continue

        draw.ellipse(bounds, fill=color)

        # Bright LEDs clip toward white in the center of a camera exposure,
        # while the outer face and halo retain the original RGB hue.
        white_mix = 0.62 * brightness**2
        highlight = tuple(
            int(round(channel * (1.0 - white_mix) + 255.0 * white_mix))
            for channel in color
        )
        inset = max(1, round(led_size * 0.27))
        left, top, right, bottom = bounds
        if left + inset <= right - inset and top + inset <= bottom - inset:
            draw.ellipse(
                (left + inset, top + inset, right - inset, bottom - inset),
                fill=highlight,
            )
    return image


def save_preview_mp4(
    animation: LedAnimation,
    path: Path,
    led_size: int = 16,
    gap: int = 0,
    preview_fps: float = 60.0,
) -> None:
    """Stream sampled preview frames into an H.264 MP4."""
    if not math.isfinite(preview_fps) or preview_fps <= 0:
        raise ValueError("preview_fps must be a positive finite number")

    # Reduce only the exported MP4 frame rate. The LedAnimation itself remains
    # untouched, so NPZ, JSON, and desktop playback keep the source frame rate.
    effective_fps = min(preview_fps, animation.fps, 60.0)
    preview_frame_count = max(1, round(animation.duration * effective_fps))
    canvas_size = gap + GRID_SIZE * (led_size + gap)
    encoded_fps = preview_frame_count / animation.duration
    ffmpeg = _require_program("ffmpeg")
    command = [
        ffmpeg,
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{canvas_size}x{canvas_size}",
        "-framerate",
        f"{encoded_fps:.12g}",
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        str(path),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    assert process.stderr is not None
    try:
        for frame_number in range(preview_frame_count):
            index = frame_number * len(animation.frames) // preview_frame_count
            image = render_led_frame(animation.frames[index], led_size, gap)
            try:
                process.stdin.write(image.tobytes())
            finally:
                image.close()
                del image
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        process.stderr.close()
        return_code = process.wait()
    except BaseException:
        if not process.stdin.closed:
            process.stdin.close()
        process.stderr.close()
        process.kill()
        process.wait()
        raise
    if return_code != 0:
        raise RuntimeError(f"FFmpeg could not create {path}: {stderr}")


def play_animation(animation: LedAnimation, led_size: int = 24, gap: int = 2) -> None:
    """Play an animation in a small Tk window using source-video timing."""
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError as exc:
        raise RuntimeError("desktop playback requires Tkinter") from exc

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"could not open a desktop window: {exc}") from exc

    root.title(f"LED Animator - {GRID_SIZE}x{GRID_SIZE} @ {animation.fps:.2f} FPS")
    label = tk.Label(root, background="black")
    label.pack()
    start = time.monotonic()
    frame_count = len(animation.frames)

    def update() -> None:
        elapsed = time.monotonic() - start
        index = int(elapsed * animation.fps) % frame_count
        photo = ImageTk.PhotoImage(render_led_frame(animation.frames[index], led_size, gap))
        label.configure(image=photo)
        label.image = photo
        next_frame = (math.floor(elapsed * animation.fps) + 1) / animation.fps
        delay_ms = max(1, round((next_frame - elapsed) * 1000))
        root.after(delay_ms, update)

    update()
    root.mainloop()


def _output_paths(prefix: Path) -> tuple[Path, Path, Path]:
    # Treat the output as a prefix even when it happens to contain a suffix.
    base = prefix.with_suffix("") if prefix.suffix else prefix
    return (
        base.parent / f"{base.name}.ledanim.npz",
        base.parent / f"{base.name}.ledmap.json",
        base.parent / f"{base.name}.preview.mp4",
    )


def build_parser(
    default_led_size: int = 16,
    default_gap: int = 0,
    default_output_suffix: str = "",
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Generate a {GRID_SIZE}x{GRID_SIZE} RGB LED animation from a video."
    )
    parser.add_argument("video", nargs="?", type=Path, help="input video")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=f"output prefix (default: INPUT_stem{default_output_suffix})",
    )
    parser.add_argument(
        "--play",
        type=Path,
        metavar="ANIMATION.npz",
        help="play an existing .ledanim.npz instead of converting a video",
    )
    parser.add_argument(
        "--preview-from",
        type=Path,
        metavar="ANIMATION.npz",
        help="create only an MP4 preview from an existing animation",
    )
    parser.add_argument("--no-json", action="store_true", help="do not write JSON LED maps")
    parser.add_argument("--no-preview", action="store_true", help="do not write the preview MP4")
    parser.add_argument(
        "--preview-after",
        action="store_true",
        help="open a live desktop preview after conversion",
    )
    parser.add_argument(
        "--led-size",
        type=int,
        default=default_led_size,
        help=f"preview LED diameter (default: {default_led_size})",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=default_gap,
        help=f"preview gap in pixels (default: {default_gap})",
    )
    parser.add_argument(
        "--preview-fps",
        "--gif-fps",
        dest="preview_fps",
        type=float,
        default=60.0,
        help="MP4-only frame rate (default/max: 60; capped at source FPS)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="frames per NPZ compression batch (default: 16)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    default_led_size: int = 16,
    default_gap: int = 0,
    default_output_suffix: str = "",
) -> int:
    args = build_parser(
        default_led_size,
        default_gap,
        default_output_suffix,
    ).parse_args(argv)
    try:
        if args.preview_from:
            if args.video or args.play:
                raise ValueError(
                    "--preview-from cannot be combined with VIDEO or --play"
                )
            if args.no_preview or args.preview_after:
                raise ValueError(
                    "--preview-from cannot be combined with --no-preview or "
                    "--preview-after"
                )
            if not args.preview_from.is_file():
                raise ValueError(
                    f"animation does not exist: {args.preview_from}"
                )

            animation_name = args.preview_from.name
            suffix = ".ledanim.npz"
            default_name = (
                animation_name[: -len(suffix)]
                if animation_name.endswith(suffix)
                else args.preview_from.stem
            )
            prefix = args.output or args.preview_from.with_name(default_name)
            _, _, preview_path = _output_paths(prefix)
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f".{preview_path.stem}.work-",
                dir=preview_path.parent,
            ) as working_directory:
                work_path = Path(working_directory)
                animation = load_npz(
                    args.preview_from,
                    work_path / "frames.npy",
                )
                try:
                    staged_preview = work_path / preview_path.name
                    save_preview_mp4(
                        animation,
                        staged_preview,
                        args.led_size,
                        args.gap,
                        args.preview_fps,
                    )
                    staged_preview.replace(preview_path)
                    print(f"Visual preview: {preview_path}")
                    print(
                        f"Rendered {len(animation.frames)} source frames at "
                        f"{animation.fps:.3f} FPS "
                        f"({animation.duration:.2f} seconds)."
                    )
                finally:
                    animation.close()
            return 0

        if args.play:
            if args.video:
                raise ValueError("VIDEO and --play cannot be used together")
            play_animation(load_npz(args.play), args.led_size, args.gap)
            return 0

        if args.video is None:
            raise ValueError("provide an input VIDEO or use --play ANIMATION.npz")
        if not args.video.is_file():
            raise ValueError(f"input video does not exist: {args.video}")
        if args.batch_size <= 0:
            raise ValueError("--batch-size must be positive")

        prefix = args.output or args.video.with_name(
            f"{args.video.stem}{default_output_suffix}"
        )
        npz_path, json_path, preview_path = _output_paths(prefix)
        npz_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix=f".{npz_path.stem}.work-",
            dir=npz_path.parent,
        ) as working_directory:
            work_path = Path(working_directory)
            frame_store_path = work_path / "frames.rgb"
            staged_npz_path = work_path / npz_path.name
            staged_json_path = work_path / json_path.name
            staged_preview_path = work_path / preview_path.name
            animation = generate_animation(args.video, frame_store_path)
            try:
                save_npz(animation, staged_npz_path, args.batch_size)
                if not args.no_json:
                    save_led_map(animation, staged_json_path)
                if not args.no_preview:
                    save_preview_mp4(
                        animation,
                        staged_preview_path,
                        args.led_size,
                        args.gap,
                        args.preview_fps,
                    )

                staged_npz_path.replace(npz_path)
                print(f"Board animation: {npz_path}")
                if not args.no_json:
                    staged_json_path.replace(json_path)
                    print(f"LED intensity map: {json_path}")
                if not args.no_preview:
                    staged_preview_path.replace(preview_path)
                    print(f"Visual preview: {preview_path}")
                print(
                    f"Generated {len(animation.frames)} frames at "
                    f"{animation.fps:.3f} FPS "
                    f"({animation.duration:.2f} seconds)."
                )
                if args.preview_after:
                    play_animation(animation, args.led_size, args.gap)
            finally:
                animation.close()
        return 0
    except (MemoryError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
