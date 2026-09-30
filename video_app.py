#!/usr/bin/env python3
"""Open the video control app without an LED board attached."""

from __future__ import annotations

import argparse
import time
import webbrowser
from pathlib import Path

from audio_palette_controls import PaletteControls, PaletteServer
from led_animator import probe_video
from stream_arduino import probe_video_duration
from video_controls import VideoPlaybackControls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview and seek a video without the LED board")
    parser.add_argument("video", type=Path, help="local MP4 file")
    parser.add_argument("--fps", type=float, default=24.0, help="intended LED playback rate")
    parser.add_argument("--start", type=float, default=0.0, help="initial time in seconds")
    parser.add_argument("--controls-port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not args.video.is_file():
        parser.error(f"video does not exist: {args.video}")
    if not 0 <= args.controls_port <= 65535:
        parser.error("--controls-port must be between 0 and 65535")
    if not 0 < args.fps <= 120:
        parser.error("--fps must be between 0 and 120")

    duration = probe_video_duration(args.video)
    fps = min(args.fps, probe_video(args.video).fps)
    controls = VideoPlaybackControls(args.video.name, duration, fps, args.start,
                                     connected=False)
    server = PaletteServer(PaletteControls(), args.controls_port,
                           video_controls=controls, video_path=args.video)
    server.start()
    print(f"Video app (LED board not required): {server.url}/#video", flush=True)
    if not args.no_browser:
        webbrowser.open(f"{server.url}/#video")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
