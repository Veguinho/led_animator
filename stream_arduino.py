#!/usr/bin/env python3
"""Convert video frames to 16x16 RGB565 and stream them over USB serial."""

from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from export_arduino import encode_rgb565
from led_animator import frame_to_led_grid, iter_square_video_frames, probe_video


WIDTH = 16
HEIGHT = 16
FRAME_BYTES = WIDTH * HEIGHT * 2
SERIAL_BAUD = 921_600

REQUEST_MAGIC = b"LEDS"
RESPONSE_MAGIC = b"LEDR"
PROTOCOL_VERSION = 1
PACKET_HELLO = 1
PACKET_FRAME = 2
PACKET_CLEAR = 3
STATUS_READY = 1
STATUS_ACK = 2
STATUS_NAK = 3

PACKET_HEADER = struct.Struct("<4sBBHII")
RESPONSE = struct.Struct("<4sBBHI")
HELLO = struct.Struct("<BBBBI")


class SerialConnection(Protocol):
    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
    def reset_input_buffer(self) -> None: ...


@dataclass(frozen=True)
class FrameSource:
    fps: float
    iter_frames: Callable[[], Iterator[bytes]]


@dataclass(frozen=True)
class DeviceResponse:
    status: int
    detail: int
    sequence: int


def build_packet(packet_type: int, sequence: int, payload: bytes = b"") -> bytes:
    """Build one checksummed host-to-controller packet."""
    if len(payload) > 0xFFFF:
        raise ValueError("packet payload is too large")
    return PACKET_HEADER.pack(
        REQUEST_MAGIC,
        PROTOCOL_VERSION,
        packet_type,
        len(payload),
        sequence & 0xFFFFFFFF,
        zlib.crc32(payload),
    ) + payload


def read_response(connection: SerialConnection, timeout: float) -> DeviceResponse:
    """Find and decode one binary response, ignoring any bootloader text."""
    deadline = time.monotonic() + timeout
    matched = 0
    while time.monotonic() < deadline:
        value = connection.read(1)
        if not value:
            continue
        byte = value[0]
        if byte == RESPONSE_MAGIC[matched]:
            matched += 1
            if matched == len(RESPONSE_MAGIC):
                break
        else:
            matched = 1 if byte == RESPONSE_MAGIC[0] else 0
    else:
        raise TimeoutError("controller did not respond")

    remainder = bytearray()
    remaining_size = RESPONSE.size - len(RESPONSE_MAGIC)
    while len(remainder) < remaining_size:
        if time.monotonic() >= deadline:
            raise TimeoutError("controller returned an incomplete response")
        remainder.extend(connection.read(remaining_size - len(remainder)))

    magic, version, status, detail, sequence = RESPONSE.unpack(
        RESPONSE_MAGIC + remainder
    )
    if magic != RESPONSE_MAGIC or version != PROTOCOL_VERSION:
        raise RuntimeError("controller returned an unsupported protocol response")
    return DeviceResponse(status, detail, sequence)


def exchange_packet(
    connection: SerialConnection,
    packet_type: int,
    sequence: int,
    payload: bytes,
    expected_status: int,
    timeout: float,
    retries: int,
) -> None:
    packet = build_packet(packet_type, sequence, payload)
    last_error: Exception | None = None
    for _attempt in range(retries + 1):
        connection.write(packet)
        connection.flush()
        try:
            while True:
                response = read_response(connection, timeout)
                if response.sequence != (sequence & 0xFFFFFFFF):
                    continue
                if response.status == STATUS_NAK:
                    raise RuntimeError(
                        f"controller rejected packet {sequence} "
                        f"(error {response.detail})"
                    )
                if response.status != expected_status:
                    raise RuntimeError(
                        f"unexpected controller status {response.status}"
                    )
                return
        except (TimeoutError, RuntimeError) as exc:
            last_error = exc
    assert last_error is not None
    raise RuntimeError(
        f"packet {sequence} failed after {retries + 1} attempts: {last_error}"
    ) from last_error


def iter_compiled_video(
    path: Path, source_fps: float, output_fps: float
) -> Iterator[bytes]:
    """Decode, sample, resize, and RGB565-encode without saving the video."""
    info = probe_video(path)
    next_output_time = 0.0
    for index, frame in enumerate(iter_square_video_frames(path, info, WIDTH)):
        frame_time = index / source_fps
        if frame_time + 1e-12 < next_output_time:
            continue
        grid = frame if frame.shape == (HEIGHT, WIDTH, 3) else frame_to_led_grid(frame)
        yield encode_rgb565(grid)
        next_output_time += 1.0 / output_fps


def open_video(path: Path, target_fps: float | None = None) -> FrameSource:
    if not path.is_file():
        raise ValueError(f"video does not exist: {path}")
    info = probe_video(path)
    if target_fps is not None and (not np.isfinite(target_fps) or target_fps <= 0):
        raise ValueError("FPS must be a positive finite number")
    fps = min(info.fps, target_fps) if target_fps is not None else info.fps
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("FPS must be a positive finite number")
    return FrameSource(
        fps=fps,
        iter_frames=lambda: iter_compiled_video(path, info.fps, fps),
    )


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is required; run: python3 -m pip install -r requirements.txt"
        ) from exc
    return [port.device for port in list_ports.comports()]


def resolve_port(requested: str) -> str:
    if requested != "auto":
        return requested
    ports = list_serial_ports()
    usb_ports = [
        port
        for port in ports
        if Path(port).name.startswith(("cu.usbmodem", "cu.usbserial"))
    ]
    callout_ports = [port for port in ports if port.startswith("/dev/cu.")]
    candidates = usb_ports or callout_ports or ports
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("no serial controller found; connect it or pass --port")
    choices = "\n  ".join(candidates)
    raise RuntimeError(
        f"multiple serial ports found; select one with --port:\n  {choices}"
    )


def open_serial(port: str, timeout: float) -> SerialConnection:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is required; run: python3 -m pip install -r requirements.txt"
        ) from exc
    return serial.Serial(
        port=port,
        baudrate=SERIAL_BAUD,
        timeout=min(0.1, timeout),
        write_timeout=timeout,
    )


def handshake(
    connection: SerialConnection, fps: float, timeout: float, retries: int
) -> None:
    frame_duration_us = round(1_000_000 / fps)
    payload = HELLO.pack(WIDTH, HEIGHT, 1, 0, frame_duration_us)
    exchange_packet(
        connection,
        PACKET_HELLO,
        0,
        payload,
        STATUS_READY,
        timeout,
        retries,
    )


def stream_frames(
    connection: SerialConnection,
    source: FrameSource,
    *,
    loop: bool,
    timeout: float,
    retries: int,
    drop_late: bool,
) -> tuple[int, int]:
    timeline_index = 0
    sent = 0
    dropped = 0
    period = 1.0 / source.fps

    while True:
        frames_this_pass = 0
        pass_start: float | None = None
        for payload in source.iter_frames():
            if len(payload) != FRAME_BYTES:
                raise ValueError(
                    f"compiled frame has {len(payload)} bytes; expected {FRAME_BYTES}"
                )
            frames_this_pass += 1
            if pass_start is None:
                # Start the clock after FFmpeg produces its first frame, so
                # process startup never causes the beginning of a clip to drop.
                pass_start = time.monotonic()
            pass_index = frames_this_pass - 1
            deadline = pass_start + pass_index * period
            now = time.monotonic()
            if drop_late and now - deadline >= period:
                dropped += 1
                timeline_index += 1
                continue
            if now < deadline:
                time.sleep(deadline - now)
            exchange_packet(
                connection,
                PACKET_FRAME,
                timeline_index + 1,
                payload,
                STATUS_ACK,
                timeout,
                retries,
            )
            sent += 1
            timeline_index += 1
        if frames_this_pass == 0:
            raise RuntimeError("video produced no frames")
        if not loop:
            return sent, dropped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an MP4 and stream it to a 16x16 ESP32 LED panel"
    )
    parser.add_argument("video", nargs="?", type=Path, help="input MP4 video")
    parser.add_argument(
        "--port", default="auto", help="serial port (default: auto-detect)"
    )
    parser.add_argument("--fps", type=float, help="optional maximum streaming FPS")
    parser.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="response timeout (default: 1 second)",
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="packet retries (default: 3)"
    )
    parser.add_argument(
        "--no-drop",
        action="store_true",
        help="slow playback instead of dropping frames if the panel falls behind",
    )
    parser.add_argument(
        "--clear-on-exit", action="store_true", help="turn off LEDs when streaming ends"
    )
    parser.add_argument(
        "--list-ports", action="store_true", help="list serial ports and exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_ports:
        for port in list_serial_ports():
            print(port)
        return 0
    if args.video is None:
        raise SystemExit("error: VIDEO.mp4 is required unless --list-ports is used")
    if args.timeout <= 0:
        raise SystemExit("error: --timeout must be positive")
    if args.retries < 0:
        raise SystemExit("error: --retries cannot be negative")

    connection: SerialConnection | None = None
    try:
        source = open_video(args.video, args.fps)
        port = resolve_port(args.port)
        print(f"Opening {port} at {SERIAL_BAUD} baud...", file=sys.stderr)
        connection = open_serial(port, args.timeout)
        # Opening USB serial often resets an ESP32. Retries cover boot and the
        # optional 1.5-second matrix coverage test in the sketch.
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, source.fps, args.timeout, max(args.retries, 3))
        print(
            f"Converting and streaming at {source.fps:.3f} FPS. Ctrl-C stops.",
            file=sys.stderr,
        )
        sent, dropped = stream_frames(
            connection,
            source,
            loop=args.loop,
            timeout=args.timeout,
            retries=args.retries,
            drop_late=not args.no_drop,
        )
        print(f"Finished: {sent} sent, {dropped} dropped.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            if args.clear_on_exit:
                try:
                    exchange_packet(
                        connection,
                        PACKET_CLEAR,
                        0xFFFFFFFF,
                        b"",
                        STATUS_ACK,
                        args.timeout,
                        0,
                    )
                except (OSError, RuntimeError):
                    pass
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
