#!/usr/bin/env python3
"""Choose live audio or buffered MP4 playback; selecting a mode installs its firmware."""

import argparse
import subprocess
import sys

from device_modes import ROOT, flash_firmware, stop_mode_workers
from stream_arduino import resolve_port


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("1. Live audio wave — 48×48, 30 FPS over USB")
        print("2. Rolling-buffer MP4 player — blended to 48×48 at 6 FPS")
        choice = input("Choose mode [1/2]: ").strip()
        if choice not in ("1", "2"):
            print("Choose 1 or 2.", file=sys.stderr)
            return 2
        args = ["audio"] if choice == "1" else ["mp4", input("Video file: ").strip()]
    if args[0] in ("-h", "--help"):
        print("Usage: main.py audio [--no-upload] [audio options]\n"
              "       main.py mp4 VIDEO [--fps 6] [--no-upload]\n"
              "Run without arguments for a menu. Switching modes uploads different firmware.\n"
              "The legacy 'video' mode name is also accepted. Use --no-upload only when "
              "the matching firmware is already installed.")
        return 0
    if args[0] in ("mp4", "video"):
        return subprocess.call([sys.executable, str(ROOT / "mp4_player/stream.py"), *args[1:]], cwd=ROOT)
    if args[0] != "audio":
        print("Mode must be audio or mp4.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Run live audio on the CH340 board")
    parser.add_argument("--port", default="auto")
    parser.add_argument("--no-upload", action="store_true")
    options, extra = parser.parse_known_args(args[1:])
    try:
        port = resolve_port(options.port, wait_timeout=30)
        if options.no_upload:
            stop_mode_workers()
        else:
            flash_firmware("audio", port)
        return subprocess.call([str(ROOT / "start.sh"), "--no-upload", "--port", port,
                                "--style", "wave", *extra], cwd=ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
