"""Discover completed local MP4s without scanning disk for every LED frame."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class LocalVideoLibrary:
    def __init__(self, root: Path = ROOT, *, refresh_seconds: float = 2.0) -> None:
        self.root = root.resolve()
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._updated = float("-inf")
        self._videos: dict[str, Path] = {}

    def videos(self, *, refresh: bool = False) -> dict[str, Path]:
        with self._lock:
            now = time.monotonic()
            if refresh or now - self._updated >= self.refresh_seconds:
                candidates = list(self.root.glob("*"))
                for folder in (self.root / "video_clips", self.root / ".build/youtube"):
                    if folder.is_dir():
                        candidates.extend(folder.rglob("*"))
                videos = {}
                for path in candidates:
                    if path.suffix.lower() != ".mp4" or re.search(r"\.(?:f\d+|temp)\.", path.name):
                        continue
                    try:
                        # Keep library selections within this checkout, including
                        # when a local folder contains links to other locations.
                        if (not path.resolve().is_relative_to(self.root)
                            or not path.is_file() or path.stat().st_size == 0):
                            continue
                    except OSError:
                        continue  # A file may disappear during a download/move.
                    videos[path.relative_to(self.root).as_posix()] = path
                self._videos = dict(sorted(videos.items(), key=lambda item: (
                    not item[0].startswith(".build/youtube/"),
                    not item[0].endswith(".square.mp4"), item[0].casefold(),
                )))
                self._updated = now
            return self._videos.copy()
