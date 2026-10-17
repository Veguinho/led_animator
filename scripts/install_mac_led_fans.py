#!/usr/bin/env python3
"""Install or remove a fixed-purpose, root-owned Intel Mac LED fan watcher.

macOS shows its own administrator authentication dialog. No password is read
or saved by this script. The helper watches only this project's LED processes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile

LABEL = "local.led-animator.fans"
HELPER = f"/Library/PrivilegedHelperTools/{LABEL}"
PLIST = f"/Library/LaunchDaemons/{LABEL}.plist"


def bootstrap_command(plist: str) -> str:
    # launchd can briefly retain the old registration after bootout completes.
    return (
        "fan_attempt=0\n"
        f"until /bin/launchctl bootstrap system {shlex.quote(plist)}; do\n"
        "    fan_attempt=$((fan_attempt + 1))\n"
        "    if [ \"$fan_attempt\" -ge 5 ]; then exit 1; fi\n"
        "    /bin/sleep 1\n"
        "done"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--build-only", action="store_true", help="compile and stage without administrator authorization")
    args = parser.parse_args()
    if args.remove and args.build_only:
        parser.error("--build-only cannot be combined with --remove")
    project = Path(__file__).resolve().parent.parent
    build = project / ".build" / "fan-control"
    build.mkdir(parents=True, exist_ok=True)
    binary = build / "led-fan-watch"
    config = Path.home() / "Library" / "Application Support" / "LED Fan Control" / "settings.plist"
    if args.remove:
        commands = [
            f"/bin/launchctl bootout system/{LABEL} 2>/dev/null || true",
            f"{shlex.quote(HELPER)} --restore",
            f"/bin/rm -f {shlex.quote(PLIST)} {shlex.quote(HELPER)}",
        ]
    else:
        subprocess.run([
            "/usr/bin/clang", "-O2", "-Wall", "-Wextra", "-Werror",
            f"-DLED_PROJECT={json.dumps(str(project))}", f"-DLED_UID={os.getuid()}",
            f"-DLED_CONFIG={json.dumps(str(config))}",
            "-framework", "IOKit", "-framework", "CoreFoundation",
            str(project / "scripts" / "macos_led_fans.c"), "-o", str(binary),
        ], check=True)
        subprocess.run([str(binary), "--status"], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(binary)], check=True)
        staged_plist = build / f"{LABEL}.plist"
        with staged_plist.open("wb") as handle:
            plistlib.dump({
                "Label": LABEL,
                "ProgramArguments": [HELPER, "--watch"],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Background",
                "ThrottleInterval": 5,
                "ExitTimeOut": 5,
                "StandardOutPath": "/var/log/led-animator-fans.log",
                "StandardErrorPath": "/var/log/led-animator-fans.log",
            }, handle)
        commands = [
            f"/bin/launchctl bootout system/{LABEL} 2>/dev/null || true",
            "/usr/bin/install -d -o root -g wheel -m 755 /Library/PrivilegedHelperTools",
            f"/usr/bin/install -o root -g wheel -m 755 {shlex.quote(str(binary))} {shlex.quote(HELPER)}",
            f"/usr/bin/install -o root -g wheel -m 644 {shlex.quote(str(staged_plist))} {shlex.quote(PLIST)}",
            bootstrap_command(PLIST),
            f"/bin/launchctl print system/{LABEL}",
        ]
    if args.build_only:
        print(f"Built: {binary}")
        return 0
    # Administrator AppleScript cannot necessarily read Documents under TCC.
    # Stage in a private directory outside Documents, with no shared filenames.
    with tempfile.TemporaryDirectory(prefix="led-fan-install-", dir="/private/tmp") as directory:
        staging = Path(directory)
        if not args.remove:
            staged_binary = staging / "led-fan-watch"
            final_plist = staging / f"{LABEL}.plist"
            shutil.copyfile(binary, staged_binary)
            shutil.copyfile(staged_plist, final_plist)
            commands = [command.replace(shlex.quote(str(binary)), shlex.quote(str(staged_binary)))
                        .replace(shlex.quote(str(staged_plist)), shlex.quote(str(final_plist)))
                        for command in commands]
        contents = "#!/bin/sh\nset -eu\n" + "\n".join(commands) + "\n"
        # Keep the exact operation in the build folder for inspection.
        (build / ("remove.sh" if args.remove else "install.sh")).write_text(contents)
        installer = staging / "install.sh"
        installer.write_text(contents)
        installer.chmod(0o700)
        shell = f"/bin/sh {shlex.quote(str(installer))}"
        print("Authenticate in the macOS administrator dialog to " + ("remove" if args.remove else "install") + " LED fan control.", flush=True)
        apple_script = "do shell script " + json.dumps(shell) + " with administrator privileges"
        result = subprocess.run(["/usr/bin/osascript", "-e", apple_script], text=True, capture_output=True)
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    print(result.stdout.strip())
    if not args.remove:
        subprocess.run([HELPER, "--status"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
