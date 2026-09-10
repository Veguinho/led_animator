#!/usr/bin/env python3
"""Build a personal macOS launcher, optionally adding it to the Dock."""

import argparse
import colorsys
import math
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
NAME = "LED Audio Visualizer"
BUNDLE_ID = "local.led-animator.audio-visualizer"


def make_icon(destination: Path) -> None:
    image = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 40, 984, 984), radius=200, fill="#17171f")
    for column in range(16):
        height = round(2 + 5 * (0.5 + 0.5 * math.sin(column * 0.6)))
        rgb = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(column / 18, 0.9, 1))
        for row in range(16):
            x, y = 145 + column * 49, 145 + row * 49
            lit = abs(row - 7.5) < height
            draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill=rgb if lit else "#2b2b38")
    image.save(destination, format="ICNS")


def pin_to_dock(app: Path) -> None:
    exported = subprocess.check_output(["defaults", "export", "com.apple.dock", "-"])
    preferences = plistlib.loads(exported)
    for item in preferences.get("persistent-apps", []):
        data = item.get("tile-data", {})
        if data.get("bundle-identifier") == BUNDLE_ID or data.get("file-data", {}).get("_CFURLString") == app.as_uri() + "/":
            print("Already in the Dock.")
            return
    backup = ROOT / ".build" / f"dock-before-app-{time.time_ns()}.plist"
    backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(exported)
    entry = {
        "tile-data": {
            "file-data": {"_CFURLString": app.as_uri() + "/", "_CFURLStringType": 15},
            "file-label": NAME, "bundle-identifier": BUNDLE_ID,
            "file-type": 41,
        },
        "tile-type": "file-tile",
    }
    subprocess.run([
        "defaults", "write", "com.apple.dock", "persistent-apps", "-array-add",
        plistlib.dumps(entry).decode(),
    ], check=True)
    subprocess.run(["killall", "Dock"], check=False, capture_output=True)
    print("Added to the Dock.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dock", action="store_true", help="also pin the app to the Dock")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("this installer requires macOS")
    python = ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        parser.error("create .venv and install requirements.txt first")
    applications = Path.home() / "Applications"
    applications.mkdir(exist_ok=True)
    destination = applications / f"{NAME}.app"
    if destination.exists():
        info = plistlib.loads((destination / "Contents" / "Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            parser.error(f"another application already exists at {destination}")

    with tempfile.TemporaryDirectory(prefix="led-app-") as temporary:
        app = Path(temporary) / destination.name
        # LaunchServices opens the .command in Terminal without Apple Events
        # automation permission. Audio access is attributed to Terminal.
        script = '''on run
    set launcher to (POSIX path of (path to me)) & "Contents/Resources/Launch.command"
    do shell script "/usr/bin/open -a Terminal " & quoted form of launcher
end run
'''
        subprocess.run(["osacompile", "-o", str(app), "-"], input=script, text=True, check=True)
        resources = app / "Contents" / "Resources"
        launcher = resources / "Launch.command"
        launcher.write_text(
            "#!/bin/bash\n"
            f"cd -- {shlex.quote(str(ROOT))} || exit 1\n"
            f"exec {shlex.quote(str(python))} {shlex.quote(str(ROOT / 'scripts' / 'launch_audio_visualizer.py'))}\n"
        )
        launcher.chmod(0o755)
        make_icon(resources / "Visualizer.icns")
        info_path = app / "Contents" / "Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update({
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": NAME,
            "CFBundleDisplayName": NAME, "CFBundleIconFile": "Visualizer",
            "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
            "LSUIElement": True,
        })
        info_path.write_bytes(plistlib.dumps(info))
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(app, destination)
    print(f"Installed: {destination}")
    if args.dock:
        pin_to_dock(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
