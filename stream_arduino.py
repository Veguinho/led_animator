#!/usr/bin/env python3
"""Convert video frames to RGB565 and stream them over USB."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
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
from video_controls import VideoPlaybackControls
from video_audio_texture import LiveAudioVideoTexture


WIDTH = 16
HEIGHT = 16
FRAME_BYTES = WIDTH * HEIGHT * 2
DEFAULT_DISPLAY_SIZE = 48
SERIAL_BAUD = 1_500_000
# Leave room for decoding and parallel LED submission as well as UART traffic.
DEFAULT_STREAM_FPS = 12.0
DEFAULT_PORT_WAIT = 30.0
VIDEO_FIRMWARE_OUTPUT_SCALE = 60
MAX_VIDEO_PIXEL_PWM_TOTAL = 4

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
PACKET_COMPRESSED_FRAME = 4
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
    iter_frames_at: Callable[[float], Iterator[bytes]] | None = None


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
    flash_limit: bool = False,
    limiter: VideoFlashLimiter | None = None,
    start_seconds: float = 0.0,
    texture: LiveAudioVideoTexture | None = None,
) -> Iterator[bytes]:
    """Decode, sample, resize, and RGB565-encode without saving the video."""
    info = probe_video(path)
    next_output_time = 0.0
    limiter = limiter or (VideoFlashLimiter(output_fps) if flash_limit else None)
    decode_options = {"start_seconds": start_seconds} if start_seconds else {}
    for index, frame in enumerate(iter_square_video_frames(path, info, size, **decode_options)):
        frame_time = index / source_fps
        if frame_time + 1e-12 < next_output_time:
            continue
        grid = frame if frame.shape == (size, size, 3) else frame_to_led_grid(frame, size)
        if texture is not None:
            grid = texture.apply(grid)
        mapped = map_led_intensity(grid, led_gamma)
        if limiter is not None:
            mapped = limiter.apply(mapped)
            if texture is not None:
                texture.publish_frame(mapped)
            yield encode_limited_video_rgb565(mapped)
        else:
            if texture is not None:
                texture.publish_frame(mapped)
            yield encode_rgb565(mapped)
        next_output_time += 1.0 / output_fps


def open_video(
    path: Path,
    target_fps: float | None = None,
    led_gamma: float = LED_INTENSITY_GAMMA,
    size: int = 16,
    flash_limit: bool = False,
    texture: LiveAudioVideoTexture | None = None,
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
    # Keep the exposure state across repeats of a short loop. Recreating it
    # each pass would dim the opening for a second every time it restarts.
    limiter = VideoFlashLimiter(fps) if flash_limit else None
    return FrameSource(
        fps=fps,
        iter_frames=lambda: iter_compiled_video(path, info.fps, fps, led_gamma, size,
                                               flash_limit, limiter, texture=texture),
        size=size,
        iter_frames_at=lambda seconds: iter_compiled_video(
            path, info.fps, fps, led_gamma, size, flash_limit, limiter,
            start_seconds=seconds, texture=texture),
    )


def probe_video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("video duration is unavailable") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("video duration is unavailable")
    return duration


def limit_video_flash(frame: np.ndarray) -> np.ndarray:
    """Bound each LED's total light and strongest channel before RGB565."""
    values = frame.astype(np.float32)
    total = values.sum(axis=2, keepdims=True)
    peak = values.max(axis=2, keepdims=True)
    strong_channels = np.count_nonzero(values >= peak * 0.45,
                                       axis=2, keepdims=True)
    # Cyan/magenta/yellow stage lights can be as dazzling as white even
    # though their third channel is dark. Give those pixels their own cap.
    # RGB565 needs a little more input for neutral pixels to reach the same
    # visible PWM step as colored pixels; their final PWM is still capped.
    limit = np.where(strong_channels == 3, 36.0,
                     np.where(strong_channels == 2, 24.0, 32.0))
    values *= np.minimum(1.0, limit / np.maximum(total, 1.0))
    np.minimum(values, 20.0, out=values)
    return np.rint(values).clip(0, 255).astype(np.uint8)


def encode_limited_video_rgb565(frame: np.ndarray) -> bytes:
    """Cap the PWM bytes the live firmware will actually transmit.

    RGB565 rounds small values in uneven steps. A limit applied only before
    encoding can therefore still leave a few harsh cyan or white pixels.
    """
    values = frame.copy()
    for _ in range(32):
        payload = encode_rgb565(values)
        packed = np.frombuffer(payload, dtype="<u2").reshape(values.shape[:2])
        red5 = (packed >> 11) & 31
        green6 = (packed >> 5) & 63
        blue5 = packed & 31
        red = (red5 << 3) | (red5 >> 2)
        green = (green6 << 2) | (green6 >> 4)
        blue = (blue5 << 3) | (blue5 >> 2)
        pwm_total = ((red * VIDEO_FIRMWARE_OUTPUT_SCALE // 255)
                     + (green * VIDEO_FIRMWARE_OUTPUT_SCALE // 255)
                     + (blue * VIDEO_FIRMWARE_OUTPUT_SCALE // 255))
        too_bright = pwm_total > MAX_VIDEO_PIXEL_PWM_TOTAL
        if not np.any(too_bright):
            return payload
        scale = (MAX_VIDEO_PIXEL_PWM_TOTAL /
                 pwm_total[too_bright].astype(np.float32))[:, None]
        values[too_bright] = np.floor(values[too_bright] * scale).astype(np.uint8)
    raise RuntimeError("video pixel PWM limit did not converge")


class VideoFlashLimiter:
    """Limit both sudden flashes and sustained bright scenes."""

    def __init__(self, fps: float):
        self.allowed_mean = 0.0
        self.max_rise = 12.0 / fps
        self.max_scene_mean = 8.0
        self.previous_frame: np.ndarray | None = None
        self.steady_frames = 0

    def apply(self, frame: np.ndarray) -> np.ndarray:
        if self.previous_frame is not None:
            motion = float(np.abs(frame.astype(np.int16) -
                                  self.previous_frame.astype(np.int16)).mean())
            self.steady_frames = self.steady_frames + 1 if motion < 2.0 else 0
        self.previous_frame = frame.copy()
        limited = limit_video_flash(frame)
        mean = float(limited.mean())
        # A stable title card can be brighter without allowing short stage
        # light bursts to grow into sustained full-panel flashes.
        scene_ceiling = 12.0 if self.steady_frames >= 8 else self.max_scene_mean
        self.allowed_mean = min(mean, scene_ceiling,
                                self.allowed_mean + self.max_rise)
        if mean > self.allowed_mean and mean > 0:
            return np.rint(limited.astype(np.float32) *
                           (self.allowed_mean / mean)).astype(np.uint8)
        return limited


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
    video_mode: bool = False,
) -> None:
    frame_duration_us = round(1_000_000 / fps)
    payload = HELLO.pack(display_size, display_size, 1, int(video_mode),
                         frame_duration_us)
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
    compress: bool = False,
    start_seconds: float = 0.0,
    controls: VideoPlaybackControls | None = None,
) -> tuple[int, int]:
    if drop_late and rebase_late:
        raise ValueError("drop_late and rebase_late cannot both be enabled")
    timeline_index = 0
    sent = 0
    dropped = 0
    resynced = 0
    period = 1.0 / source.fps
    stats_start = time.monotonic()
    stats_sent = 0
    pass_start_seconds = start_seconds

    while True:
        frames_this_pass = 0
        pass_start: float | None = None
        seek_to: float | None = None
        if pass_start_seconds and source.iter_frames_at is None:
            raise ValueError("this frame source does not support seeking")
        frames = (source.iter_frames_at(pass_start_seconds)
                  if source.iter_frames_at is not None else source.iter_frames())
        try:
            for payload in frames:
                if controls is not None:
                    seek_to = controls.take_seek()
                    if seek_to is not None:
                        break
                payload = resize_rgb565(payload, source.size, display_size)
                frames_this_pass += 1
                if controls is not None:
                    controls.set_position(pass_start_seconds + (frames_this_pass - 1) * period)
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
                        continue
                if now < deadline:
                    time.sleep(deadline - now)
                compressed = zlib.compress(payload, 3) if compress else payload
                packet_type = (PACKET_COMPRESSED_FRAME
                               if len(compressed) < len(payload) else PACKET_FRAME)
                exchange_packet(
                    connection,
                    packet_type,
                    timeline_index + 1,
                    compressed if packet_type == PACKET_COMPRESSED_FRAME else payload,
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
        finally:
            close = getattr(frames, "close", None)
            if close is not None:
                close()
        if seek_to is not None:
            pass_start_seconds = seek_to
            continue
        if frames_this_pass == 0:
            raise RuntimeError("video produced no frames")
        if not loop:
            if controls is not None:
                controls.finish()
            return sent, dropped
        pass_start_seconds = 0.0


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
        default=None,
        help="maximum streaming FPS (default: video's original frame rate)",
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
    parser.add_argument("--no-flash-limit", action="store_true",
                        help="disable 48x48 video highlight and transition limiting")
    parser.add_argument("--no-audio-reactive", action="store_true",
                        help="play the video's original colors without live system audio")
    parser.add_argument("--no-compression", action="store_true",
                        help="send uncompressed frames (for older firmware)")
    parser.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    parser.add_argument("--start", type=float, default=0.0,
                        help="begin at this time in the video, in seconds")
    parser.add_argument("--controls-port", type=int, default=8765,
                        help="local video control panel port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the local video control panel")
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
    if not 0 <= args.controls_port <= 65535:
        raise SystemExit("error: --controls-port must be between 0 and 65535")

    connection: SerialConnection | None = None
    control_server = None
    capture = None
    try:
        duration = probe_video_duration(args.video)
        video_fps = probe_video(args.video).fps
        controls = VideoPlaybackControls(
            args.video.name, duration,
            min(args.fps, video_fps) if args.fps is not None else video_fps,
            args.start,
        )
        from audio_palette_controls import PaletteControls, PaletteServer, default_settings
        palette_path = Path(__file__).resolve().parent / ".build" / "audio-palette.json"
        palette_controls = PaletteControls(
            size=args.display_size,
            settings_path=palette_path if not args.no_audio_reactive else None,
        )
        texture = None
        if not args.no_audio_reactive:
            from system_audio_visualizer import AudioCapture
            capture = AudioCapture()
            capture.start()
            if not palette_path.is_file():
                palette_controls.update(default_settings() | {
                    "preset": "adaptive", "brightness": 1.0,
                })
            texture = LiveAudioVideoTexture(
                palette_controls, controls, capture.latest, size=args.display_size,
            )
            print("Live Mac system audio is coloring video and changing saturation.",
                  file=sys.stderr)
        source = open_video(args.video, args.fps, args.led_gamma, args.display_size,
                            flash_limit=args.display_size == 48 and not args.no_flash_limit,
                            texture=texture)
        port = resolve_port(args.port, wait_timeout=args.port_wait)
        print(f"Opening {port} for {args.display_size}×{args.display_size} frames...", file=sys.stderr)
        connection = open_serial(port, args.timeout, baudrate=args.baud)
        # Opening USB serial often resets an ESP32. Retries cover boot and the
        # optional 1.5-second matrix coverage test in the sketch.
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, source.fps, args.timeout, max(args.retries, 3),
                  args.display_size,
                  video_mode=args.display_size == 48 and not args.no_flash_limit)
        control_server = PaletteServer(palette_controls, args.controls_port,
                                       video_controls=controls, video_path=args.video)
        control_server.start()
        print(f"Video controls: {control_server.url}/#video", file=sys.stderr)
        if not args.no_browser:
            import webbrowser
            webbrowser.open(f"{control_server.url}/#video")
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
            compress=args.display_size == 48 and not args.no_compression,
            start_seconds=args.start,
            controls=controls,
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
        if control_server is not None:
            control_server.close()
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
        if capture is not None:
            capture.close()


if __name__ == "__main__":
    raise SystemExit(main())
