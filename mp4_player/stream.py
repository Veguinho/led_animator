#!/usr/bin/env python3
"""Stream an MP4 through a bounded frame buffer to the 48x48 LED board."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from device_modes import flash_firmware, stop_mode_workers
from mp4_player.playback_controls import PlaybackControls, PlaybackControlServer
from mp4_player.prepare import enhance_prepared_video, prepare_video
from stream_arduino import resolve_port
import stream_arduino


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--port", default="auto")
    parser.add_argument("--no-upload", action="store_true",
                        help="the 48x48 streaming firmware is already installed")
    parser.add_argument("--fps", type=float, default=6.0,
                        help="streaming frame rate (default: stable 6 FPS)")
    parser.add_argument("--buffer-seconds", type=float, default=3.0)
    parser.add_argument("--prebuffer-seconds", type=float, default=1.0)
    parser.add_argument("--led-gamma", type=float, default=1.0,
                        help="LED intensity gamma after offline enhancement (default: 1)")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--timeout", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--no-drop", action="store_true")
    parser.add_argument("--clear-on-exit", action="store_true")
    parser.add_argument("--controls-port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser


def stream_with_resume(forwarded: list[str], controls: PlaybackControls) -> int:
    """Reconnect without replaying frames already acknowledged by the board."""
    next_frame = 0

    def remember_progress(frame: int) -> None:
        nonlocal next_frame
        next_frame = frame

    while True:
        controls.set_connection("connecting")
        result = stream_arduino.main(
            [*forwarded, "--start-frame", str(next_frame)],
            playback_paused=controls.paused,
            connection_status=controls.set_connection,
            progress_callback=remember_progress,
        )
        if result in (0, 130):
            return result
        controls.set_connection("reconnecting")
        print(
            f"LED stream disconnected; resuming at frame {next_frame} in 2 seconds…",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = None
    try:
        prepared = enhance_prepared_video(
            prepare_video(args.video.expanduser(), args.fps, ROOT / ".build/mp4-cache")
        )
        port = resolve_port(args.port, wait_timeout=30)
        if args.no_upload:
            stop_mode_workers()
        else:
            flash_firmware("stream", port)
        controls = PlaybackControls()
        server = PlaybackControlServer(controls, args.controls_port)
        server.start()
        print(f"Video Play/Pause controls: {server.url}", file=sys.stderr, flush=True)
        if not args.no_browser:
            try:
                webbrowser.open(server.url)
            except OSError:
                pass
        forwarded = [
            str(prepared),
            "--port", port,
            "--display-size", "48",
            "--fps", str(args.fps),
            "--buffer-seconds", str(args.buffer_seconds),
            "--prebuffer-seconds", str(args.prebuffer_seconds),
            "--led-gamma", str(args.led_gamma),
            "--baud", str(args.baud),
            "--timeout", str(args.timeout),
            "--retries", str(args.retries),
        ]
        if args.loop:
            forwarded.append("--loop")
        if args.no_drop:
            forwarded.append("--no-drop")
        if args.clear_on_exit:
            forwarded.append("--clear-on-exit")
        return stream_with_resume(forwarded, controls)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.close()


if __name__ == "__main__":
    raise SystemExit(main())
