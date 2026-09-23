#!/usr/bin/env python3
"""Prepare a clip, preload it over CH340, and start playback from ESP32 PSRAM."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

import numpy as np

# Also support `python preloaded_video/player.py ...`.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from device_modes import flash_firmware, stop_mode_workers
from export_arduino import encode_rgb565
from led_animator import map_led_intensity
from stream_arduino import DEFAULT_DISPLAY_SIZE, open_serial, resolve_port

DISPLAY_SIZE = DEFAULT_DISPLAY_SIZE
FRAME_BYTES = DISPLAY_SIZE * DISPLAY_SIZE * 2
MAX_CLIP_BYTES = 6 * 1024 * 1024
CHUNK_BYTES = 4096
HEADER = struct.Struct("<4sBBHII")
PROTOCOL_MAGIC = b"PV48"
RESPONSE = struct.Struct("<4sBBHIIII")
INFO, BEGIN, CHUNK, PLAY, STOP, STATUS = range(1, 7)
BAUD, VERIFY = 7, 8
BOOT_BAUD = 230400
DEFAULT_BAUD = 2000000
BAUD_RATES = (230400, 460800, 921600, 1000000, 1500000, 2000000)
ERRORS = {
    1: "bad packet", 2: "packet CRC mismatch", 3: "clip exceeds available PSRAM",
    4: "unexpected chunk offset", 5: "clip incomplete or checksum mismatch",
    6: "PSRAM unavailable; build this firmware with PSRAM=opi",
}


@dataclass(frozen=True)
class Clip:
    data: bytes
    fps: int

    @property
    def frames(self) -> int:
        return len(self.data) // FRAME_BYTES


def soften_frames(frames: np.ndarray, fps: int, brightness: float,
                  smooth_ms: float) -> np.ndarray:
    """Dim LED intensities and apply a periodic low-pass filter across the loop."""
    if not math.isfinite(brightness) or not 0 <= brightness <= 1:
        raise ValueError("brightness must be between 0 and 1")
    if not math.isfinite(smooth_ms) or smooth_ms < 0:
        raise ValueError("smooth-ms must be non-negative")
    values = frames.astype(np.float32) * brightness
    if smooth_ms and len(values):
        alpha = -math.expm1(-1000 / (fps * smooth_ms))
        state = np.zeros_like(values[0])
        for frame in values:
            state += alpha * (frame - state)
        # Solve the periodic initial state so the last/first frame seam also
        # receives smoothing instead of an abrupt reset at every loop.
        state /= -math.expm1(-len(values) * 1000 / (fps * smooth_ms))
        for index, frame in enumerate(values):
            state += alpha * (frame - state)
            values[index] = state
    return np.rint(values).clip(0, 255).astype(np.uint8)


def prepare_clip(path: Path, fps: int, seconds: float | None = None,
                 start: float = 0, gamma: float = 2.2,
                 brightness: float = 0.25, smooth_ms: float = 180) -> Clip:
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
    command = [ffmpeg, "-v", "error", "-nostdin", "-ss", str(start), "-i", str(path)]
    if seconds is not None:
        command += ["-t", str(seconds)]
    command += ["-map", "0:v:0", "-an", "-vf",
                f"crop=min(iw\\,ih):min(iw\\,ih),scale={DISPLAY_SIZE}:{DISPLAY_SIZE}:flags=area,fps={fps}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    frames = []
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        assert process.stdout is not None
        try:
            while True:
                raw = process.stdout.read(DISPLAY_SIZE * DISPLAY_SIZE * 3)
                if not raw:
                    break
                if len(raw) != DISPLAY_SIZE * DISPLAY_SIZE * 3:
                    raise RuntimeError("FFmpeg returned a truncated frame")
                if (len(frames) + 1) * FRAME_BYTES > MAX_CLIP_BYTES:
                    maximum = (MAX_CLIP_BYTES // FRAME_BYTES) / fps
                    raise ValueError(f"clip is too long; use --seconds below {maximum:.2f} at {fps} FPS")
                rgb = np.frombuffer(raw, dtype=np.uint8).reshape(DISPLAY_SIZE, DISPLAY_SIZE, 3)
                frames.append(map_led_intensity(rgb, gamma))
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


def read_response(connection, timeout: float):
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    while time.monotonic() < deadline:
        buffer.extend(connection.read(1))
        if buffer[-4:] != b"PVOK":
            if len(buffer) > 4:
                del buffer[:-4]
            continue
        buffer = bytearray(b"PVOK")
        while len(buffer) < RESPONSE.size and time.monotonic() < deadline:
            buffer.extend(connection.read(RESPONSE.size - len(buffer)))
        if len(buffer) != RESPONSE.size:
            break
        return RESPONSE.unpack(buffer)
    raise TimeoutError("no preloaded-video response; install the video firmware (omit --no-upload)")


def response_for(connection, sequence: int):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = read_response(connection, max(0.001, deadline - time.monotonic()))
        _, version, status, detail, received_sequence, value, displayed, missed = response
        if received_sequence != sequence:
            continue
        if version != 1:
            raise RuntimeError("unsupported video protocol")
        if status != 1 and detail == 2:
            raise TimeoutError("packet CRC mismatch")
        if status != 1:
            raise RuntimeError(ERRORS.get(detail, f"board error {detail}"))
        return value, displayed, missed
    raise TimeoutError("only stale video responses received")


def packet_bytes(kind: int, sequence: int, payload: bytes):
    return HEADER.pack(PROTOCOL_MAGIC, 1, kind, len(payload), sequence, zlib.crc32(payload)) + payload


def exchange(connection, kind: int, sequence: int, payload: bytes = b"", retries: int = 3):
    packet = packet_bytes(kind, sequence, payload)
    error = None
    for _ in range(retries + 1):
        connection.write(packet)
        # Reading the ACK already waits for the full request to reach the board.
        try:
            return response_for(connection, sequence)
        except TimeoutError as exc:
            error = exc
    raise RuntimeError(str(error))


def set_baud(connection, baudrate: int):
    if connection.baudrate == baudrate:
        return
    # The board always boots at the old speed, including after flashing.
    exchange(connection, BAUD, 0xffff0000, struct.pack("<I", baudrate), retries=0)
    time.sleep(0.05)
    try:
        connection.baudrate = baudrate
        exchange(connection, INFO, 0xffff0001, retries=0)
    except (OSError, RuntimeError, ValueError) as exc:
        # Allow the firmware's unconfirmed-speed timeout to restore boot baud.
        time.sleep(2)
        connection.baudrate = BOOT_BAUD
        connection.reset_input_buffer()
        raise RuntimeError(f"could not confirm {baudrate} baud; retry with --baud {BOOT_BAUD}") from exc


def connect_video(port: str, baudrate: int):
    connection = open_serial(port, 2, baudrate=BOOT_BAUD)
    try:
        time.sleep(0.5)
        # Some USB bridges reset on open; others retain the previous speed.
        for rate in dict.fromkeys((BOOT_BAUD, baudrate, *BAUD_RATES)):
            connection.baudrate = rate
            connection.reset_input_buffer()
            try:
                exchange(connection, INFO, 0xfffe0000, retries=0)
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError("video firmware did not respond; run without --no-upload")
        exchange(connection, STOP, 0xfffe0001)
        set_baud(connection, baudrate)
        return connection
    except Exception:
        connection.close()
        raise


def preload(connection, clip: Clip, loop: bool = True, *, play: bool = True):
    capacity, _, _ = exchange(connection, INFO, 1)
    if len(clip.data) > capacity:
        raise ValueError(f"clip needs {len(clip.data)} bytes; board offers {capacity}. Shorten with --seconds.")
    exchange(connection, BEGIN, 2, struct.pack("<III", clip.frames, clip.fps, zlib.crc32(clip.data)))
    sequence = 3
    last_percent = -1
    for offset in range(0, len(clip.data), CHUNK_BYTES):
        chunk = clip.data[offset:offset + CHUNK_BYTES]
        received, _, _ = exchange(connection, CHUNK, sequence, struct.pack("<I", offset) + chunk)
        if received != offset + len(chunk):
            raise RuntimeError("board reported an inconsistent upload offset")
        sequence += 1
        percent = received * 100 // len(clip.data)
        if percent // 10 != last_percent // 10:
            print(f"Preloading: {percent}%", flush=True)
            last_percent = percent
    exchange(connection, PLAY if play else VERIFY, sequence, bytes([loop]) if play else b"")
    return sequence + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--fps", type=int, default=30, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--seconds", type=float, help="clip duration; omit to use the whole file if it fits")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--led-gamma", type=float, default=2.2)
    parser.add_argument("--brightness", type=float, default=0.25,
                        help="pixel intensity multiplier, 0..1 (default: 0.25)")
    parser.add_argument("--smooth-ms", type=float, default=180,
                        help="temporal smoothing in milliseconds; 0 disables (default: 180)")
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, choices=BAUD_RATES, default=DEFAULT_BAUD,
                        help="video transfer baud rate (default: %(default)s)")
    parser.add_argument("--no-upload", action="store_true", help="video firmware is already installed")
    parser.add_argument("--prepare-only", action="store_true", help="check conversion/capacity without touching the board")
    parser.add_argument("--once", action="store_true", help="play once and hold the last frame")
    parser.add_argument("--detach", action="store_true", help="leave playback running without monitoring")
    args = parser.parse_args(argv)
    connection = None
    sequence = 1
    try:
        clip = prepare_clip(args.video.expanduser(), args.fps, args.seconds, args.start,
                            args.led_gamma, args.brightness, args.smooth_ms)
        print(f"Prepared {clip.frames} frames at {clip.fps} FPS ({clip.frames/clip.fps:.2f} s), "
              f"{len(clip.data):,} bytes. USB preload takes at least {len(clip.data)/(args.baud/10):.0f} seconds.")
        if args.prepare_only:
            return 0
        port = resolve_port(args.port, wait_timeout=30)
        if args.no_upload:
            stop_mode_workers()
        else:
            flash_firmware("video", port)
        connection = connect_video(port, args.baud)
        print(f"Video connection: {args.baud:,} baud", flush=True)
        upload_start = time.monotonic()
        sequence = preload(connection, clip, loop=not args.once)
        upload_seconds = time.monotonic() - upload_start
        print(f"Preloaded in {upload_seconds:.1f} s ({len(clip.data)/upload_seconds:,.0f} bytes/s)", flush=True)
        print(f"Playing from board memory at a target of {clip.fps} FPS. "
              "The clip is lost on reset/power loss.", flush=True)
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
                fps = (displayed - previous[1]) / (now - previous[0])
                print(f"Playback: {fps:.1f} FPS; {missed} skipped deadlines total", flush=True)
            previous = now, displayed
            if not playing:
                return 0
    except KeyboardInterrupt:
        if connection is not None:
            try:
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
