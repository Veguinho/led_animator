#!/usr/bin/env python3
"""Terminal entry point for the clickable macOS application."""

import fcntl
from pathlib import Path
import sys
import webbrowser


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    # The OS releases this lock even if the process crashes. Repeated Dock
    # clicks must not start competing streams on the same serial connection.
    (ROOT / ".build").mkdir(exist_ok=True)
    with (ROOT / ".build" / "desktop-app.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("LED Audio Visualizer is already starting or running.")
            print("Controls: http://127.0.0.1:8765 (available once streaming starts)")
            webbrowser.open("http://127.0.0.1:8765")
            return 0

        sys.path.insert(0, str(ROOT))
        from system_audio_visualizer import main as run_visualizer

        print("LED Audio Visualizer — live spectrum and rainbow palette")
        print("Connect the LED board by USB and play audio on your Mac.")
        print("Press Control-C in this window to stop and turn off the LEDs.\n")
        return run_visualizer(["--style", "spectrum", "--clear-on-exit"])


if __name__ == "__main__":
    raise SystemExit(main())
