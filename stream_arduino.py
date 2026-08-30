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
from led_animator import (
    LED_INTENSITY_GAMMA,
    frame_to_led_grid,
    iter_square_video_frames,
    map_led_intensity,
    probe_video,
)


WIDTH = 16
HEIGHT = 16
FRAME_BYTES = WIDTH * HEIGHT * 2
SERIAL_BAUD = 230_400
DEFAULT_STREAM_FPS = 30.0
DEFAULT_PORT_WAIT = 30.0

# Common USB serial names on macOS, Linux, and boards using WCH or Silicon Labs
# USB-to-UART chips. Restricting automatic selection to these names prevents a
# Mac's Bluetooth headphones and speakers from being mistaken for the panel.
USB_SERIAL_PREFIXES = (
    "cu.usbmodem",
    "cu.usbserial",
    "cu.wchusbserial",
    "cu.SLAB_USBtoUART",
    "tty.usbmodem",
    "tty.usbserial",
    "tty.wchusbserial",
    "tty.SLAB_USBtoUART",
    "ttyACM",
    "ttyUSB",
)

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
    path: Path,
    source_fps: float,
    output_fps: float,
    led_gamma: float = LED_INTENSITY_GAMMA,
) -> Iterator[bytes]:
    """Decode, sample, resize, and RGB565-encode without saving the video."""
    info = probe_video(path)
    next_output_time = 0.0
    for index, frame in enumerate(iter_square_video_frames(path, info, WIDTH)):
        frame_time = index / source_fps
        if frame_time + 1e-12 < next_output_time:
            continue
        grid = frame if frame.shape == (HEIGHT, WIDTH, 3) else frame_to_led_grid(frame)
        yield encode_rgb565(map_led_intensity(grid, led_gamma))
        next_output_time += 1.0 / output_fps


def open_video(
    path: Path,
    target_fps: float | None = None,
    led_gamma: float = LED_INTENSITY_GAMMA,
) -> FrameSource:
    if not path.is_file():
        raise ValueError(f"video does not exist: {path}")
    info = probe_video(path)
    if target_fps is not None and (not np.isfinite(target_fps) or target_fps <= 0):
        raise ValueError("FPS must be a positive finite number")
    fps = min(info.fps, target_fps) if target_fps is not None else info.fps
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("FPS must be a positive finite number")
    if not np.isfinite(led_gamma) or led_gamma <= 0:
        raise ValueError("LED gamma must be a positive finite number")
    return FrameSource(
        fps=fps,
        iter_frames=lambda: iter_compiled_video(path, info.fps, fps, led_gamma),
    )


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is required; run: python3 -m pip install -r requirements.txt"
        ) from exc
    return [port.device for port in list_ports.comports()]


def usb_serial_ports(ports: list[str]) -> list[str]:
    """Return only ports whose device names identify USB serial hardware."""
    return [
        port
        for port in ports
        if Path(port).name.startswith(USB_SERIAL_PREFIXES)
    ]


def resolve_port(
    requested: str,
    *,
    wait_timeout: float = 0.0,
    poll_interval: float = 0.25,
) -> str:
    """Resolve an explicit port or wait for one USB controller to appear."""
    if requested != "auto":
        return requested

    deadline = time.monotonic() + wait_timeout
    announced_wait = False
    while True:
        candidates = usb_serial_ports(list_serial_ports())
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            choices = "\n  ".join(candidates)
            raise RuntimeError(
                "multiple USB serial controllers found; select one with --port:\n"
                f"  {choices}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "no USB serial controller found; reconnect the board or pass --port"
            )
        if not announced_wait:
            print(
                f"Waiting up to {wait_timeout:g} seconds for a USB serial "
                "controller. Connect the board...",
                file=sys.stderr,
            )
            announced_wait = True
        time.sleep(poll_interval)


def open_serial(port: str, timeout: float) -> SerialConnection:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is required; run: python3 -m pip install -r requirements.txt"
        ) from exc
    # Configure DTR/RTS before opening. Their pyserial defaults are asserted,
    # which can hold some ESP32-S3 auto-reset circuits in reset indefinitely.
    connection = serial.Serial()
    connection.port = port
    connection.baudrate = SERIAL_BAUD
    connection.timeout = min(0.1, timeout)
    connection.write_timeout = timeout
    connection.dtr = False
    connection.rts = False
    connection.open()
    return connection


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
    parser.add_argument(
        "--port-wait",
        type=float,
        default=DEFAULT_PORT_WAIT,
        help="seconds to wait for an auto-detected board (default: 30)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_STREAM_FPS,
        help="maximum streaming FPS (default: 30)",
    )
    parser.add_argument(
        "--led-gamma",
        type=float,
        default=LED_INTENSITY_GAMMA,
        help=(
            "LED intensity curve; higher values make dark pixels dimmer "
            "(default: 2.2)"
        ),
    )
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
    if args.port_wait < 0:
        raise SystemExit("error: --port-wait cannot be negative")
    if args.retries < 0:
        raise SystemExit("error: --retries cannot be negative")

    connection: SerialConnection | None = None
    try:
        source = open_video(args.video, args.fps, args.led_gamma)
        port = resolve_port(args.port, wait_timeout=args.port_wait)
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
