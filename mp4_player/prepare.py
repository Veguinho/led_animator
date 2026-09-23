"""Pre-render a high-contrast 48x48 MP4 once for low-CPU LED streaming."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess


FILTER_VERSION = "contrast-v1"
DISPLAY_FILTER_VERSION = "luma-s-curve-sat-v3"
DISPLAY_FILTER = (
    "curves=master='0/0 0.20/0.14 0.50/0.42 0.75/0.84 0.90/0.98 1/1',"
    "eq=saturation=1.18"
)


def prepared_path(source: Path, fps: float, cache_root: Path) -> Path:
    stat = source.stat()
    fps_key = f"{float(fps):g}"
    identity = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{fps_key}|{FILTER_VERSION}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    safe_stem = "".join(character if character.isalnum() else "_" for character in source.stem)
    return cache_root / f"{safe_stem[:48]}-48x48-{fps_key}fps-{digest}.mp4"


def prepare_video(source: Path, fps: float, cache_root: Path) -> Path:
    if not source.is_file():
        raise ValueError(f"video does not exist: {source}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to prepare video")
    output = prepared_path(source, fps, cache_root)
    if output.is_file() and output.stat().st_size:
        print(f"Using prepared LED video: {output}", flush=True)
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".partial.mp4")
    filters = (
        "crop=min(iw\\,ih):min(iw\\,ih),"
        "scale=48:48:flags=area,"
        "colorlevels=rimin=0.03:gimin=0.03:bimin=0.03:"
        "rimax=0.90:gimax=0.90:bimax=0.90,"
        "eq=contrast=1.35:saturation=1.45:brightness=0.02,"
        f"fps={fps:g}"
    )
    print("Preparing high-contrast 48×48 video once; playback starts when this finishes…", flush=True)
    try:
        subprocess.run([
            ffmpeg, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", filters, "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
        ], check=True)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"Prepared LED video: {output}", flush=True)
    return output


def enhance_prepared_video(source: Path) -> Path:
    """Apply an offline LED S-curve that darkens mids and lifts highlights."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to enhance video")
    stat = source.stat()
    identity = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{DISPLAY_FILTER_VERSION}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    output = source.with_name(f"{source.stem}-high-dynamic-{digest}.mp4")
    if output.is_file() and output.stat().st_size:
        print(f"Using high-dynamic LED video: {output}", flush=True)
        return output
    temporary = output.with_name(output.stem + ".partial.mp4")
    print("Applying the LED highlight S-curve once…", flush=True)
    try:
        subprocess.run([
            ffmpeg, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", DISPLAY_FILTER,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "15",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
        ], check=True)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"Prepared high-dynamic LED video: {output}", flush=True)
    return output
