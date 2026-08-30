#!/usr/bin/env python3
"""Export 16x16 LED animations to a compact Arduino-friendly binary."""

from __future__ import annotations

import argparse
import json
import re
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np


MAGIC = b"LEDANIM\0"
FORMAT_VERSION = 1
PIXEL_FORMAT_RGB565_LE = 1
HEADER = struct.Struct("<8sBBBBIII")


@dataclass(frozen=True)
class ArduinoAnimation:
    frames: np.ndarray
    fps: float

    def __post_init__(self) -> None:
        if self.frames.ndim != 4 or self.frames.shape[1:] != (16, 16, 3):
            raise ValueError(
                "Arduino export requires frames shaped (count, 16, 16, 3), "
                f"got {self.frames.shape}"
            )
        if self.frames.dtype != np.uint8:
            raise ValueError(f"frames must use uint8 channels, got {self.frames.dtype}")
        if len(self.frames) == 0:
            raise ValueError("animation must contain at least one frame")
        if not np.isfinite(self.fps) or self.fps <= 0:
            raise ValueError("fps must be a positive finite number")


def load_animation(path: Path) -> ArduinoAnimation:
    if path.name.endswith(".ledanim.npz") or path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as data:
            frames = np.asarray(data["frames"], dtype=np.uint8)
            fps = float(data["fps"])
        return ArduinoAnimation(frames, fps)

    if path.name.endswith(".ledmap.json") or path.suffix == ".json":
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
        width = int(data["width"])
        height = int(data["height"])
        if (width, height) != (16, 16):
            raise ValueError(f"Arduino export requires a 16x16 map, got {width}x{height}")
        frames = np.asarray(data["frames"], dtype=np.uint8)
        return ArduinoAnimation(frames, float(data["fps"]))

    raise ValueError("input must be a .ledmap.json or .ledanim.npz file")


def sample_animation(
    animation: ArduinoAnimation, target_fps: float | None
) -> ArduinoAnimation:
    if target_fps is None or target_fps >= animation.fps:
        return animation
    if not np.isfinite(target_fps) or target_fps <= 0:
        raise ValueError("target FPS must be a positive finite number")

    duration = len(animation.frames) / animation.fps
    output_count = max(1, round(duration * target_fps))
    indices = (
        np.arange(output_count, dtype=np.uint64) * len(animation.frames) // output_count
    )
    effective_fps = output_count / duration
    return ArduinoAnimation(np.ascontiguousarray(animation.frames[indices]), effective_fps)


def encode_rgb565(frames: np.ndarray) -> bytes:
    channels = frames.astype(np.uint16)
    packed = (
        ((channels[..., 0] >> 3) << 11)
        | ((channels[..., 1] >> 2) << 5)
        | (channels[..., 2] >> 3)
    )
    return packed.astype("<u2", copy=False).tobytes(order="C")


def build_binary(animation: ArduinoAnimation) -> bytes:
    payload = encode_rgb565(animation.frames)
    frame_duration_us = round(1_000_000 / animation.fps)
    header = HEADER.pack(
        MAGIC,
        FORMAT_VERSION,
        16,
        16,
        PIXEL_FORMAT_RGB565_LE,
        len(animation.frames),
        frame_duration_us,
        zlib.crc32(payload),
    )
    return header + payload


def write_atomic(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(contents)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
    path.chmod(0o644)


def validate_symbol(symbol: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        raise ValueError(f"invalid C++ symbol: {symbol}")
    return symbol


def write_header(path: Path, binary: bytes, symbol: str) -> None:
    symbol = validate_symbol(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", dir=path.parent, delete=False
    ) as output:
        output.write("#pragma once\n\n")
        output.write("#include <Arduino.h>\n\n")
        output.write(f"const uint8_t {symbol}[] PROGMEM = {{\n")
        for offset in range(0, len(binary), 16):
            values = ", ".join(f"0x{value:02x}" for value in binary[offset : offset + 16])
            output.write(f"  {values},\n")
        output.write("};\n")
        output.write(f"constexpr size_t {symbol}_size = sizeof({symbol});\n")
        temporary_path = Path(output.name)
    temporary_path.replace(path)
    path.chmod(0o644)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a 16x16 LED map as RGB565 data for Arduino/ESP32"
    )
    parser.add_argument("input", type=Path, help="16x16 .ledmap.json or .ledanim.npz")
    parser.add_argument("-o", "--output", required=True, type=Path, help="output .ledbin")
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="optional lower playback FPS (frames are sampled across the full duration)",
    )
    parser.add_argument("--header", type=Path, help="also emit a PROGMEM C++ header")
    parser.add_argument(
        "--symbol", default="led_animation", help="C++ array name used with --header"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        animation = sample_animation(load_animation(args.input), args.fps)
        binary = build_binary(animation)
        write_atomic(args.output, binary)
        if args.header:
            write_header(args.header, binary, args.symbol)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"Arduino binary: {args.output}")
    if args.header:
        print(f"PROGMEM header: {args.header}")
    print(
        f"Exported {len(animation.frames)} RGB565 frames at {animation.fps:.3f} FPS "
        f"({len(binary)} bytes including the {HEADER.size}-byte header)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
