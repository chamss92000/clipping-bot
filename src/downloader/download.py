"""Bloc 2 — Téléchargement des clips via yt-dlp.

Les clips Twitch sont courts (≤ 60 s) : on télécharge toujours le clip
entier, plafonné à DOWNLOAD_MAX_HEIGHT (1080p suffit pour un rendu 1080x1920).

  * `download(candidate, dest_dir)` -> `DownloadResult` (chemin + métadonnées réelles)
  * `probe_media(path)`             -> (durée, largeur, hauteur) via ffprobe
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from config import settings
from src.models import VideoCandidate
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("downloader")


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadResult:
    candidate: VideoCandidate
    path: Path
    duration_s: float
    width: int
    height: int


def probe_media(path: str | Path) -> tuple[float, int, int]:
    """Retourne (durée_s, width, height) via ffprobe."""
    cmd = [
        settings.FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height", "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise DownloadError(f"ffprobe a échoué sur {path} : {exc}") from exc
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [{}]) or [{}]
    duration = float((data.get("format") or {}).get("duration", 0.0) or 0.0)
    return duration, int(streams[0].get("width", 0) or 0), int(streams[0].get("height", 0) or 0)


def _ydl_opts(dest_dir: Path) -> dict:
    h = settings.DOWNLOAD_MAX_HEIGHT
    return {
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/best",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": settings.DOWNLOAD_RETRIES,
        "fragment_retries": settings.DOWNLOAD_RETRIES,
        "concurrent_fragment_downloads": settings.DOWNLOAD_CONCURRENCY,
        "restrictfilenames": True,
    }


@retry(exceptions=(yt_dlp.utils.DownloadError, DownloadError), attempts=3)
def _run_ydl(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise DownloadError(f"yt-dlp n'a rien renvoyé pour {url}")
    return info


def _resolve_output_path(info: dict, dest_dir: Path) -> Path:
    reqs = info.get("requested_downloads")
    if reqs and reqs[0].get("filepath"):
        return Path(reqs[0]["filepath"])
    fid = info.get("id", "video")
    found = list(dest_dir.glob(f"{fid}*.mp4")) or list(dest_dir.glob(f"{fid}*"))
    if not found:
        raise DownloadError(f"Fichier introuvable après téléchargement (id={fid})")
    return found[0]


def download(candidate: VideoCandidate, dest_dir: str | Path) -> DownloadResult:
    """Télécharge un clip et retourne ses métadonnées réelles (DownloadError sinon)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info("Téléchargement %s", candidate.uid)
    info = _run_ydl(candidate.url, _ydl_opts(dest_dir))
    path = _resolve_output_path(info, dest_dir)
    duration, width, height = probe_media(path)
    if duration <= 0 or width <= 0 or height <= 0:
        raise DownloadError(f"Téléchargement vide/invalide ({duration:.0f}s {width}x{height})")
    log.info("OK %s : %.0fs %dx%d (%s)", candidate.uid, duration, width, height, path.name)
    return DownloadResult(candidate=candidate, path=path, duration_s=duration, width=width, height=height)


if __name__ == "__main__":  # python -m src.downloader.download <url>
    import sys

    from src.models import Platform

    if len(sys.argv) < 2:
        print("usage: python -m src.downloader.download <url_du_clip_twitch>")
        raise SystemExit(2)
    cand = VideoCandidate(platform=Platform.TWITCH, source_id="test", url=sys.argv[1],
                          title="test", creator="test")
    print(download(cand, settings.DOWNLOAD_DIR))
