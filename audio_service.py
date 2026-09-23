"""Manage the per-user macOS service that keeps live audio streaming alive."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
LABEL = "com.led-animator.audio"


def service_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def installed() -> bool:
    path = service_path()
    if not path.exists():
        return False
    with path.open("rb") as source:
        return plistlib.load(source).get("WorkingDirectory") == str(ROOT)


def running() -> bool:
    return subprocess.run(["launchctl", "print", target()], capture_output=True).returncode == 0


def stop() -> None:
    # Called before uploads/mode switches, so KeepAlive cannot reopen the port.
    if installed() and running():
        subprocess.run(["launchctl", "bootout", target()], check=True)


def start(port: str = "auto") -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the background service requires macOS")
    path = service_path()
    if path.exists() and not installed():
        raise RuntimeError(f"{path} belongs to another checkout")
    if installed() and running():
        return
    # Import here to avoid a cycle: stop_mode_workers also stops this service.
    from device_modes import stop_mode_workers
    stop_mode_workers()
    logs = ROOT / ".build"
    logs.mkdir(exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [
            str(ROOT / ".venv/bin/python"), str(ROOT / "system_audio_visualizer.py"),
            "--style", "spectrum", "--display-size", "48", "--fps", "30",
            "--port", port, "--clear-on-exit", "--no-browser",
            "--settings-file", str(logs / "audio-palette.json"),
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ExitTimeOut": 10,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(logs / "audio-service.log"),
        "StandardErrorPath": str(logs / "audio-service.log"),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(config))
    temporary.replace(path)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "uninstall"))
    parser.add_argument("--port", default="auto")
    args = parser.parse_args()
    if args.action == "start":
        start(args.port)
        print("Background audio service started. Controls: http://127.0.0.1:8765")
    elif args.action in ("stop", "uninstall"):
        stop()
        if args.action == "uninstall" and installed():
            service_path().unlink()
        print("Background audio service stopped.")
    else:
        subprocess.run(["launchctl", "print", target()], check=True)


if __name__ == "__main__":
    main()
