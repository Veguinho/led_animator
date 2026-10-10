"""Prepare low-resolution YouTube videos for the existing local MP4 player."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from led_animator import _require_program


CACHE_DIR = Path(__file__).resolve().parent / ".build" / "youtube"


def name_youtube_video(path: Path, title: str, video_id: str) -> Path:
    """Give a completed download a readable, filesystem-safe title."""
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', " ", title)
    title = " ".join(title.split()).strip(". ") or "YouTube video"
    # Leave room for the ID and extension within a 255-byte filename, including
    # titles containing accented characters or emoji.
    title = title.encode("utf-8")[:200].decode("utf-8", errors="ignore").rstrip(". ")
    destination = path.with_name(f"{title} [{video_id}].mp4")
    if path == destination:
        return path
    if destination.exists():
        raise FileExistsError(f"video already exists: {destination}")
    path.rename(destination)
    return destination


def youtube_url(value: object) -> str:
    """Accept individual YouTube videos, stripping playlists and other options."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Cole um link de vídeo do YouTube.")
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        raise ValueError("Use um link https://www.youtube.com/watch?v=… ou https://youtu.be/…")
    host = parsed.netloc.lower()
    parts = parsed.path.strip("/").split("/")
    video_id = None
    if host == "youtu.be" and len(parts) == 1:
        video_id = parts[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) == 2 and parts[0] in ("shorts", "embed", "live"):
            video_id = parts[1]
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Cole o link de um vídeo individual do YouTube.")
    return f"https://www.youtube.com/watch?v={video_id}"


def download_youtube(value: str, *, cache_dir: Path = CACHE_DIR,
                     progress: Callable[[str], None] | None = None) -> Path:
    """Download at most 360p, keeping audio and reusing completed MP4s."""
    url = youtube_url(value)
    video_id = parse_qs(urlparse(url).query)["v"][0]
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{video_id}.mp4"
    # The ID keeps cache lookup unambiguous even when titles collide or change.
    named = sorted(path for path in cache_dir.glob("*.mp4")
                   if path.name.endswith(f" [{video_id}].mp4"))
    for cached in [*named, destination]:
        if cached.is_file() and cached.stat().st_size:
            if progress:
                progress("Vídeo em cache. Iniciando…")
            return cached
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise RuntimeError('Instale o suporte ao YouTube: python -m pip install -r requirements.txt') from exc
    ffmpeg = _require_program("ffmpeg")
    last_message = ""

    def update(event: dict) -> None:
        nonlocal last_message
        if progress is None:
            return
        message = ""
        if event.get("status") == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            percent = min(100, int(100 * event.get("downloaded_bytes", 0) / total)) if total else None
            message = f"Baixando YouTube em até 360p{f' · {percent}%' if percent is not None else ''}…"
        elif event.get("status") == "finished":
            message = "Preparando MP4…"
        if message and message != last_message:
            last_message = message
            progress(message)

    # H.264 + AAC keeps the existing browser preview compatible. Do not fall
    # back to unrestricted resolutions when a small format is unavailable.
    options = {
        "format": "bv[height<=360][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[height<=360][ext=mp4][vcodec^=avc1]",
        "outtmpl": str(cache_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "ffmpeg_location": str(Path(ffmpeg).parent),
        "noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False,
        "socket_timeout": 20, "retries": 3, "fragment_retries": 3,
        "progress_hooks": [update],
    }
    # GUI launches may lack Homebrew's PATH. yt-dlp enables Deno by default;
    # explicitly enable Node too when that is the installed JS runtime.
    runtimes = {}
    for name in ("deno", "node"):
        location = shutil.which(name) or shutil.which(name, path="/opt/homebrew/bin:/usr/local/bin")
        if location:
            runtimes[name] = {"path": location}
    if runtimes:
        options["js_runtimes"] = runtimes
    try:
        with YoutubeDL(options) as downloader:
            if progress:
                progress("Consultando vídeo do YouTube…")
            info = downloader.extract_info(url, download=False)
            if not info or info.get("_type") in ("playlist", "multi_video"):
                raise RuntimeError("Escolha um vídeo individual do YouTube.")
            duration = info.get("duration")
            if (info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")
                or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
                raise RuntimeError("Use um vídeo gravado; transmissões ao vivo não podem ser baixadas neste modo.")
            downloader.process_info(info)
    except DownloadError as exc:
        raise RuntimeError(f"Não foi possível baixar o YouTube: {exc}") from exc
    if not destination.is_file() or not destination.stat().st_size:
        raise RuntimeError("O download não produziu um MP4. Tente outro vídeo.")
    return name_youtube_video(destination, info.get("title") or "YouTube video", video_id)
