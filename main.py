#!/usr/bin/env python3
"""Choose live audio or preloaded video; selecting a mode installs its firmware."""

import argparse
import subprocess
import sys

from device_modes import ROOT, flash_firmware, stop_mode_workers
from stream_arduino import resolve_port


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("1. Live audio wave — 48×48, 12 FPS over USB")
        print("2. Preloaded video — 48×48, dimmed and smoothed at 30 FPS")
        print("3. YouTube — download up to 360p, then stream at 48×48")
        choice = input("Choose mode [1/2/3]: ").strip()
        if choice not in ("1", "2", "3"):
            print("Choose 1, 2 or 3.", file=sys.stderr)
            return 2
        args = (["audio"] if choice == "1" else
                ["video", input("Video file: ").strip()] if choice == "2" else
                ["youtube", input("YouTube URL: ").strip()])
    if args[0] in ("-h", "--help"):
        print("Usage: main.py audio [--no-upload] [audio options]\n"
              "       main.py video VIDEO [--seconds N] [--fps 30] [--no-upload]\n"
              "       main.py youtube URL [--no-upload] [video streaming options]\n"
              "Run without arguments for a menu. Switching modes uploads different firmware.\n"
              "Use --no-upload only when the matching firmware is already installed.")
        return 0
    if args[0] == "video":
        return subprocess.call([sys.executable, str(ROOT / "preloaded_video/player.py"), *args[1:]], cwd=ROOT)
    if args[0] not in ("audio", "youtube"):
        print("Mode must be audio, video or youtube.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Run live audio on the CH340 board")
    parser.add_argument("--port", default="auto")
    parser.add_argument("--no-upload", action="store_true")
    if args[0] == "youtube":
        parser.add_argument("url")
    options, extra = parser.parse_known_args(args[1:])
    try:
        if args[0] == "youtube":
            from youtube_video import download_youtube
            video = download_youtube(options.url, progress=lambda message: print(message, flush=True))
        port = resolve_port(options.port, wait_timeout=30)
        if options.no_upload:
            stop_mode_workers()
        else:
            flash_firmware("audio", port)
        if args[0] == "youtube":
            return subprocess.call([sys.executable, str(ROOT / "stream_arduino.py"),
                                    str(video), "--port", port, "--display-size", "48",
                                    "--loop", "--clear-on-exit", *extra], cwd=ROOT)
        return subprocess.call([str(ROOT / "start.sh"), "--no-upload", "--port", port,
                                "--style", "wave", *extra], cwd=ROOT)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
