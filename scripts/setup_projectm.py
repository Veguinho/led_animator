#!/usr/bin/env python3
"""Fetch pinned projectM sources/presets and build the local LED renderer."""
from pathlib import Path
import argparse
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".build/projectm"
REPOSITORIES = {
    "frontend-sdl-cpp": ("frontend-sdl-cpp", "4f729b07abd71b6cd304e81b0adc708beb98a622"),
    "projectm": ("projectm", "3158ee615eaafd93a8912b5f6dd84a9c47b2e00a"),
    "cream-of-the-crop": ("presets-cream-of-the-crop", "0180df21f5e0bd39b9060cc5de420ed2f1f9e509"),
    "presets": ("presets-milkdrop-texture-pack", "6368812f27bc747b517218fbf89d21d59afce4d9"),
}


def run(args, **kwargs):
    subprocess.run([str(value) for value in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    for directory, (repository, revision) in REPOSITORIES.items():
        destination = WORK / "vendor" / directory
        if not destination.exists():
            destination.mkdir(parents=True)
            run(["git", "init", destination])
            run(["git", "-C", destination, "remote", "add", "origin",
                 f"https://github.com/projectM-visualizer/{repository}.git"])
            run(["git", "-C", destination, "fetch", "--depth", "1", "origin", revision])
            run(["git", "-C", destination, "checkout", "--detach", "FETCH_HEAD"])
        actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision:
            raise SystemExit(f"Unexpected revision in {destination}: {actual}; expected {revision}")
        if directory in ("projectm", "frontend-sdl-cpp"):
            run(["git", "-C", destination, "submodule", "update", "--init", "--recursive"])
    run(["cmake", "-S", ROOT / "projectm", "-B", WORK / "build",
         f"-DPROJECTM_SOURCE={WORK / 'vendor/projectm'}", "-DCMAKE_BUILD_TYPE=Release",
         "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"])
    run(["cmake", "--build", WORK / "build", "--parallel", args.jobs])
    print("projectM is ready. Open the LED app's projectM tab at http://127.0.0.1:8765/#projectm")


if __name__ == "__main__":
    main()
