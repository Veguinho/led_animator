#!/usr/bin/env python3
"""Convert video frames to RGB565 and stream them over USB."""

from __future__ import annotations

import argparse
import itertools
import math
import queue
import struct
import sys
import threading
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
DEFAULT_DISPLAY_SIZE = 48
SERIAL_BAUD = 2_000_000
# UART reception overlaps the ESP32's hardware-timed LED transmission.
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
    size: int = 16


@dataclass(frozen=True)
class DeviceResponse:
    status: int
    detail: int
    sequence: int


@dataclass(frozen=True)
class _ProducerFailure:
    error: BaseException


_BUFFER_END = object()


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
            print(f"Serial retry for packet {sequence}: {exc}", file=sys.stderr, flush=True)
    assert last_error is not None
    raise RuntimeError(
        f"packet {sequence} failed after {retries + 1} attempts: {last_error}"
    ) from last_error


def iter_compiled_video(
    path: Path,
    source_fps: float,
    output_fps: float,
    led_gamma: float = LED_INTENSITY_GAMMA,
    size: int = 16,
) -> Iterator[bytes]:
    """Decode, sample, resize, and RGB565-encode without saving the video."""
    info = probe_video(path)
    next_output_time = 0.0
    for index, frame in enumerate(iter_square_video_frames(path, info, size)):
        frame_time = index / source_fps
        if frame_time + 1e-12 < next_output_time:
            continue
        grid = frame if frame.shape == (size, size, 3) else frame_to_led_grid(frame, size)
        yield encode_rgb565(map_led_intensity(grid, led_gamma))
        next_output_time += 1.0 / output_fps


def open_video(
    path: Path,
    target_fps: float | None = None,
    led_gamma: float = LED_INTENSITY_GAMMA,
    size: int = 16,
    start_frame: int = 0,
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
    if start_frame < 0:
        raise ValueError("start frame cannot be negative")
    first_pass = True

    def iter_frames() -> Iterator[bytes]:
        nonlocal first_pass
        skip = start_frame if first_pass else 0
        first_pass = False
        frames = iter_compiled_video(path, info.fps, fps, led_gamma, size)
        return itertools.islice(frames, skip, None)

    return FrameSource(
        fps=fps,
        iter_frames=iter_frames,
        size=size,
    )


def buffered_source(
    source: FrameSource,
    buffer_seconds: float,
    prebuffer_seconds: float,
) -> FrameSource:
    """Decode into a bounded queue so short host stalls do not pause playback."""
    if not np.isfinite(buffer_seconds) or buffer_seconds <= 0:
        raise ValueError("buffer seconds must be positive")
    if not np.isfinite(prebuffer_seconds) or prebuffer_seconds < 0:
        raise ValueError("prebuffer seconds cannot be negative")
    if prebuffer_seconds > buffer_seconds:
        raise ValueError("prebuffer seconds cannot exceed buffer seconds")
    capacity = max(1, math.ceil(source.fps * buffer_seconds))
    prebuffer = min(capacity, math.ceil(source.fps * prebuffer_seconds))

    def iter_frames() -> Iterator[bytes]:
        pending: queue.Queue[bytes | _ProducerFailure | object] = queue.Queue(capacity)
        ready = threading.Event()
        stopped = threading.Event()

        def enqueue(item: bytes | _ProducerFailure | object) -> bool:
            while not stopped.is_set():
                try:
                    pending.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    pass
            return False

        def produce() -> None:
            frames = source.iter_frames()
            try:
                for frame in frames:
                    if not enqueue(frame):
                        break
                    if pending.qsize() >= prebuffer:
                        ready.set()
            except BaseException as exc:
                enqueue(_ProducerFailure(exc))
            finally:
                close = getattr(frames, "close", None)
                if close is not None:
                    close()
                enqueue(_BUFFER_END)
                ready.set()

        worker = threading.Thread(target=produce, name="mp4-frame-decoder", daemon=True)
        worker.start()
        ready.wait()
        buffered = min(pending.qsize(), capacity)
        print(
            f"Frame buffer ready: {buffered}/{capacity} frames "
            f"({buffered / source.fps:.2f} s).",
            file=sys.stderr,
            flush=True,
        )
        try:
            while True:
                item = pending.get()
                if item is _BUFFER_END:
                    return
                if isinstance(item, _ProducerFailure):
                    raise item.error
                assert isinstance(item, bytes)
                yield item
        finally:
            stopped.set()
            while worker.is_alive():
                try:
                    pending.get_nowait()
                except queue.Empty:
                    pass
                worker.join(timeout=0.1)

    return FrameSource(fps=source.fps, iter_frames=iter_frames, size=source.size)


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


def open_serial(port: str, timeout: float, baudrate: int = SERIAL_BAUD) -> SerialConnection:
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
    connection.baudrate = baudrate
    connection.timeout = min(0.1, timeout)
    connection.write_timeout = timeout
    connection.dtr = False
    connection.rts = False
    connection.open()
    return connection


def handshake(
    connection: SerialConnection, fps: float, timeout: float, retries: int,
    display_size: int = 16,
) -> None:
    frame_duration_us = round(1_000_000 / fps)
    payload = HELLO.pack(display_size, display_size, 1, 0, frame_duration_us)
    exchange_packet(
        connection,
        PACKET_HELLO,
        0,
        payload,
        STATUS_READY,
        timeout,
        retries,
    )


def resize_rgb565(payload: bytes, source_size: int, display_size: int) -> bytes:
    """Scale existing effects to the tiled display without changing RGB565 colors."""
    expected = source_size * source_size * 2
    if len(payload) != expected:
        raise ValueError(f"compiled frame has {len(payload)} bytes; expected {expected}")
    if source_size == display_size:
        return payload
    pixels = np.frombuffer(payload, dtype="<u2").reshape(source_size, source_size)
    indices = np.arange(display_size) * source_size // display_size
    return pixels[indices[:, None], indices[None, :]].astype("<u2").tobytes()


def add_display_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baud", type=int, default=SERIAL_BAUD,
                        choices=(230400, 460800, 921600, 1000000, 1500000, 2000000),
                        help="serial baud rate; must match firmware (default: %(default)s)")
    parser.add_argument(
        "--display-size", type=int, choices=(16, 32, 48), default=DEFAULT_DISPLAY_SIZE,
        help="physical screen width/height (default: %(default)s; must match the installed firmware)",
    )


def stream_frames(
    connection: SerialConnection,
    source: FrameSource,
    *,
    loop: bool,
    timeout: float,
    retries: int,
    drop_late: bool,
    rebase_late: bool = False,
    display_size: int = 16,
    playback_paused: Callable[[], bool] | None = None,
    source_start_frame: int = 0,
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[int, int]:
    if drop_late and rebase_late:
        raise ValueError("drop_late and rebase_late cannot both be enabled")
    timeline_index = 0
    source_frame_index = source_start_frame
    sent = 0
    dropped = 0
    resynced = 0
    period = 1.0 / source.fps
    stats_start = time.monotonic()
    stats_sent = 0

    while True:
        frames_this_pass = 0
        pass_start: float | None = None
        for payload in source.iter_frames():
            payload = resize_rgb565(payload, source.size, display_size)
            frames_this_pass += 1
            resumed = False
            while playback_paused is not None and playback_paused():
                resumed = True
                time.sleep(0.05)
            if resumed and pass_start is not None:
                # Paused time is not part of the video timeline. Rebase so
                # resume sends the held next frame instead of dropping ahead.
                pass_start = time.monotonic() - (frames_this_pass - 1) / source.fps
            if pass_start is None:
                # Start the clock after FFmpeg produces its first frame, so
                # process startup never causes the beginning of a clip to drop.
                pass_start = time.monotonic()
            pass_index = frames_this_pass - 1
            deadline = pass_start + pass_index * period
            now = time.monotonic()
            if now - deadline >= period:
                if rebase_late:
                    # A live/procedural source has no timeline worth catching
                    # up. Keep the last valid panel frame during the pause,
                    # send this newly rendered frame once, and establish a new
                    # evenly spaced clock. This avoids CPU stalls turning into
                    # bursts of discarded animation states and visible jumps.
                    resynced += max(1, int((now - deadline) // period))
                    pass_start = now - pass_index * period
                    deadline = now
                elif drop_late:
                    dropped += 1
                    timeline_index += 1
                    source_frame_index += 1
                    if progress_callback is not None:
                        progress_callback(source_frame_index)
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
            if sent == 1:
                print(
                    f"Controller acknowledged the first {display_size}×{display_size} frame.",
                    file=sys.stderr,
                )
            stats_now = time.monotonic()
            if stats_now - stats_start >= 5:
                timing = f"{dropped} late frames skipped total"
                if rebase_late:
                    timing = f"{resynced} late intervals smoothly resynchronized total"
                print(
                    f"Live playback: {(sent - stats_sent)/(stats_now - stats_start):.1f} FPS; "
                    f"{timing}",
                    file=sys.stderr,
                    flush=True,
                )
                stats_start, stats_sent = stats_now, sent
            timeline_index += 1
            source_frame_index += 1
            if progress_callback is not None:
                progress_callback(source_frame_index)
        if frames_this_pass == 0:
            if source_frame_index > 0 and not loop:
                return sent, dropped
            raise RuntimeError("video produced no frames")
        if not loop:
            return sent, dropped
        source_frame_index = 0
        if progress_callback is not None:
            progress_callback(source_frame_index)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an MP4 and stream it to an ESP32 LED screen"
    )
    add_display_argument(parser)
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
        help="maximum streaming FPS (default: 30 with pipelined 48x48 output)",
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
    parser.add_argument(
        "--buffer-seconds", type=float, default=3.0,
        help="bounded decoded-frame buffer in seconds (default: 3)",
    )
    parser.add_argument(
        "--prebuffer-seconds", type=float, default=1.0,
        help="frames to prepare before playback begins (default: 1)",
    )
    parser.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    parser.add_argument(
        "--start-frame", type=int, default=0,
        help="zero-based output frame to resume from (default: 0)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.2,
        help="response timeout (default: 0.2 seconds)",
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


def main(
    argv: list[str] | None = None,
    *,
    playback_paused: Callable[[], bool] | None = None,
    connection_status: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
) -> int:
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
    if args.start_frame < 0:
        raise SystemExit("error: --start-frame cannot be negative")
    if args.buffer_seconds <= 0:
        raise SystemExit("error: --buffer-seconds must be positive")
    if args.prebuffer_seconds < 0 or args.prebuffer_seconds > args.buffer_seconds:
        raise SystemExit(
            "error: --prebuffer-seconds must be non-negative and no larger than the buffer"
        )

    connection: SerialConnection | None = None
    try:
        source = buffered_source(
            open_video(
                args.video,
                args.fps,
                args.led_gamma,
                args.display_size,
                start_frame=args.start_frame,
            ),
            args.buffer_seconds,
            args.prebuffer_seconds,
        )
        port = resolve_port(args.port, wait_timeout=args.port_wait)
        print(f"Opening {port} for {args.display_size}×{args.display_size} frames...", file=sys.stderr)
        connection = open_serial(port, args.timeout, baudrate=args.baud)
        # Opening USB serial often resets an ESP32. Retries cover boot and the
        # optional 1.5-second matrix coverage test in the sketch.
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, source.fps, args.timeout, max(args.retries, 3), args.display_size)
        if connection_status is not None:
            connection_status("live")
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
            display_size=args.display_size,
            playback_paused=playback_paused,
            source_start_frame=args.start_frame,
            progress_callback=progress_callback,
        )
        print(f"Finished: {sent} sent, {dropped} dropped.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        if connection_status is not None:
            connection_status("reconnecting")
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
