#!/usr/bin/env python3
"""Build and install the native LED Fan Control app, shortcut, and Dock entry."""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from install_mac_led_fans import bootstrap_command

ROOT = Path(__file__).resolve().parent.parent
NAME = "LED Fan Control"
BUNDLE_ID = "local.led-animator.fan-control"
HELPER_LABEL = "local.led-animator.fans"


def pin_to_dock(app: Path) -> None:
    exported = subprocess.check_output(["/usr/bin/defaults", "export", "com.apple.dock", "-"])
    preferences = plistlib.loads(exported)
    for entry in preferences.get("persistent-apps", []):
        tile = entry.get("tile-data", {})
        if tile.get("bundle-identifier") == BUNDLE_ID or tile.get("file-data", {}).get("_CFURLString") == app.as_uri() + "/":
            print("Fan control is already pinned to the Dock.")
            return
    backup = ROOT / ".build" / "fan-control" / f"dock-before-{time.time_ns()}.plist"
    backup.write_bytes(exported)
    entry = {"tile-type": "file-tile", "tile-data": {
        "bundle-identifier": BUNDLE_ID, "file-label": NAME, "file-type": 41,
        "file-data": {"_CFURLString": app.as_uri() + "/", "_CFURLStringType": 15},
    }}
    subprocess.run(["/usr/bin/defaults", "write", "com.apple.dock", "persistent-apps", "-array-add", plistlib.dumps(entry).decode()], check=True)
    subprocess.run(["/usr/bin/killall", "Dock"], check=False, capture_output=True)
    print("Pinned fan control to the Dock.")


def desktop_shortcut(app: Path) -> None:
    shortcut = Path.home() / "Desktop" / f"{NAME}.app"
    if shortcut.is_symlink() and shortcut.resolve() == app.resolve():
        print(f"Desktop shortcut already exists: {shortcut}")
        return
    if shortcut.exists() or shortcut.is_symlink():
        raise RuntimeError(f"A different file already exists at {shortcut}; left it intact.")
    shortcut.symlink_to(app, target_is_directory=True)
    print(f"Desktop shortcut: {shortcut}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dock", action="store_true")
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--install-helper", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    destination = Path.home() / "Applications" / f"{NAME}.app"
    if destination.exists():
        info = plistlib.loads((destination / "Contents" / "Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            parser.error(f"A different application exists at {destination}.")
    build = ROOT / ".build" / "fan-control"
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "install_mac_led_fans.py"), "--build-only"], check=True)
    with tempfile.TemporaryDirectory(prefix="led-fan-app-", dir="/private/tmp") as temporary:
        app = Path(temporary) / destination.name
        executable = app / "Contents" / "MacOS" / "LEDFanControl"
        resources = app / "Contents" / "Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir(parents=True)
        subprocess.run(["/usr/bin/swiftc", "-swift-version", "5", "-O", "-framework", "SwiftUI", "-framework", "AppKit",
                        str(ROOT / "scripts" / "mac_fan_control.swift"), "-o", str(executable)], check=True)
        subprocess.run([str(executable), "--self-test"], check=True)
        # Render the app's own vector fan symbol into standard macOS icon sizes.
        png = Path(temporary) / "fan.png"
        subprocess.run([str(executable), "--icon", str(png)], check=True)
        iconset = Path(temporary) / "FanControl.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                path = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run(["/usr/bin/sips", "-z", str(size * scale), str(size * scale), str(png), "--out", str(path)], check=True, capture_output=True)
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(resources / "FanControl.icns")], check=True)
        shutil.copy2(build / "led-fan-watch", resources / "led-fan-watch")
        shutil.copy2(build / f"{HELPER_LABEL}.plist", resources / f"{HELPER_LABEL}.plist")
        final_resources = destination / "Contents" / "Resources"
        helper = f"/Library/PrivilegedHelperTools/{HELPER_LABEL}"
        daemon = f"/Library/LaunchDaemons/{HELPER_LABEL}.plist"
        commands = [
            f"/bin/launchctl bootout system/{HELPER_LABEL} 2>/dev/null || true",
            "/usr/bin/install -d -o root -g wheel -m 755 /Library/PrivilegedHelperTools",
            f"/usr/bin/install -o root -g wheel -m 755 {shlex.quote(str(final_resources / 'led-fan-watch'))} {shlex.quote(helper)}",
            f"/usr/bin/install -o root -g wheel -m 644 {shlex.quote(str(final_resources / (HELPER_LABEL + '.plist')))} {shlex.quote(daemon)}",
            bootstrap_command(daemon),
        ]
        (resources / "InstallHelper.sh").write_text("#!/bin/sh\nset -eu\n" + "\n".join(commands) + "\n")
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": NAME, "CFBundleDisplayName": NAME,
            "CFBundleExecutable": "LEDFanControl", "CFBundlePackageType": "APPL",
            "CFBundleIconFile": "FanControl", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication", "LSApplicationCategoryType": "public.app-category.utilities",
        }))
        subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, capture_output=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(app, destination)
    print(f"Installed: {destination}")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(destination)], check=True)
    if args.desktop:
        desktop_shortcut(destination)
    if args.dock:
        pin_to_dock(destination)
    if args.install_helper:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "install_mac_led_fans.py")], check=True)
    if not args.no_open:
        subprocess.run(["/usr/bin/open", str(destination)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
