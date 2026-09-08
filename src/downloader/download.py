"""Bloc 2 — Téléchargement via yt-dlp.

Fonctionnalités :
  * `download(candidate, dest_dir, start=None, end=None)` : télécharge la vidéo
    entière, ou seulement la fenêtre temporelle [start, end] (via
    `download_ranges` de yt-dlp + découpe aux keyframes).
  * `plan_windows(duration_s)` : applique la stratégie "longs VODs" — renvoie
    soit `[None]` (télécharger tout), soit une liste de fenêtres (start, end)
    à traiter indépendamment pour borner le coût sur les subathons.
  * `probe_media(path)` : durée/résolution réelles via ffprobe.

Qualité : on plafonne à `DOWNLOAD_MAX_HEIGHT` (1080p) — inutile de récupérer de
la 4K pour un rendu vertical 1080x1920, ça économise disque et bande passante.

yt-dlp gère nativement les extracteurs YouTube, Twitch (VOD) et Kick (VOD).
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
    #: Fenêtre source (start, end) si extrait partiel, sinon None (vidéo entière).
    window: tuple[float, float] | None = None


# ---------------------------------------------------------------------------
# Stratégie "longs VODs"
# ---------------------------------------------------------------------------
def plan_windows(duration_s: float | None) -> list[tuple[float, float] | None]:
    """Décide comment couvrir une source selon sa durée.

    :returns:
        * ``[None]``                -> télécharger l'intégralité ;
        * ``[(s1,e1), (s2,e2)...]`` -> échantillonner ces fenêtres ;
        * ``[]``                    -> source à ignorer (garde-fou dur dépassé).
    """
    if duration_s is None:
        # Durée inconnue (cas Kick) : on tente l'intégralité, le cap se fera au
        # probe post-download si besoin.
        return [None]

    if settings.SOURCE_HARD_MAX_DURATION_S and duration_s > settings.SOURCE_HARD_MAX_DURATION_S:
        log.info(
            "Source ignorée (%.0fs > garde-fou %ds)",
            duration_s,
            settings.SOURCE_HARD_MAX_DURATION_S,
        )
        return []

    if duration_s <= settings.SOURCE_FULL_MAX_DURATION_S:
        return [None]

    # Échantillonnage : N fenêtres réparties uniformément dans la zone utile.
    win = settings.SAMPLE_WINDOW_S
    start_zone = settings.SAMPLE_SKIP_INTRO_S
    end_zone = max(start_zone + win, duration_s - settings.SAMPLE_SKIP_OUTRO_S)
    usable = end_zone - start_zone
    n = max(1, settings.SAMPLE_WINDOWS)

    if usable <= win:
        return [(float(start_zone), float(start_zone + win))]

    windows: list[tuple[float, float]] = []
    if n == 1:
        centers = [start_zone + usable / 2]
    else:
        step = (usable - win) / (n - 1)
        centers = [start_zone + i * step for i in range(n)]
    for c in centers:
        s = max(0.0, min(c, duration_s - win))
        windows.append((round(s, 2), round(s + win, 2)))
    log.info("VOD long (%.0fs) => %d fenêtres de %ds : %s", duration_s, len(windows), win, windows)
    return windows  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------
def probe_media(path: str | Path) -> tuple[float, int, int]:
    """Retourne (durée_s, width, height) via ffprobe."""
    path = str(path)
    cmd = [
        settings.FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "json",
        path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise DownloadError(f"ffprobe a échoué sur {path} : {exc}") from exc
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [{}]) or [{}]
    duration = float(fmt.get("duration", 0.0) or 0.0)
    width = int(streams[0].get("width", 0) or 0)
    height = int(streams[0].get("height", 0) or 0)
    return duration, width, height


# ---------------------------------------------------------------------------
# Téléchargement
# ---------------------------------------------------------------------------
def _ydl_opts(dest_dir: Path, outtmpl: str, window: tuple[float, float] | None) -> dict:
    h = settings.DOWNLOAD_MAX_HEIGHT
    opts: dict = {
        # bestvideo<=1080p + bestaudio, fallback progressif.
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/best",
        "outtmpl": str(dest_dir / outtmpl),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": settings.DOWNLOAD_RETRIES,
        "fragment_retries": settings.DOWNLOAD_RETRIES,
        "concurrent_fragment_downloads": settings.DOWNLOAD_CONCURRENCY,
        "restrictfilenames": True,
        "ignoreerrors": False,
    }
    if window is not None:
        start, end = window
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
        # Recoupe précisément aux keyframes (sinon décalage de début possible).
        opts["force_keyframes_at_cuts"] = True
    return opts


@retry(exceptions=(yt_dlp.utils.DownloadError, DownloadError), attempts=3)
def _run_ydl(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise DownloadError(f"yt-dlp n'a rien renvoyé pour {url}")
    return info


def _resolve_output_path(info: dict, dest_dir: Path) -> Path:
    """Récupère le chemin réel du fichier produit par yt-dlp."""
    reqs = info.get("requested_downloads")
    if reqs and reqs[0].get("filepath"):
        return Path(reqs[0]["filepath"])
    # Fallback : reconstruire depuis l'id (outtmpl basé sur l'id).
    fid = info.get("id", "video")
    candidates = list(dest_dir.glob(f"{fid}*.mp4")) or list(dest_dir.glob(f"{fid}*"))
    if not candidates:
        raise DownloadError(f"Fichier introuvable après téléchargement (id={fid})")
    return candidates[0]


def download(
    candidate: VideoCandidate,
    dest_dir: str | Path,
    start: float | None = None,
    end: float | None = None,
) -> DownloadResult:
    """Télécharge une vidéo (ou une fenêtre) et retourne ses métadonnées réelles.

    Lève `DownloadError` (déjà retryé) en cas d'échec définitif.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    window = (start, end) if (start is not None and end is not None) else None
    suffix = f"_{int(start)}-{int(end)}" if window else ""
    outtmpl = f"%(id)s{suffix}.%(ext)s"

    log.info(
        "Téléchargement %s%s -> %s",
        candidate.uid,
        f" [{start}-{end}]" if window else "",
        dest_dir,
    )
    info = _run_ydl(candidate.url, _ydl_opts(dest_dir, outtmpl, window))
    path = _resolve_output_path(info, dest_dir)

    duration, width, height = probe_media(path)
    # Un extrait vide/corrompu (ex. fenêtre en toute fin de VOD indisponible)
    # revient en 0s / 0x0 : on rejette pour ne pas planter la transcription.
    if duration <= 0 or width <= 0 or height <= 0:
        raise DownloadError(
            f"Téléchargement vide/invalide ({duration:.0f}s {width}x{height}) — fenêtre ignorée"
        )
    log.info("OK %s : %.0fs %dx%d (%s)", candidate.uid, duration, width, height, path.name)

    return DownloadResult(
        candidate=candidate,
        path=path,
        duration_s=duration,
        width=width,
        height=height,
        window=window,
    )


def download_planned(candidate: VideoCandidate, dest_dir: str | Path) -> list[DownloadResult]:
    """Télécharge une source selon `plan_windows` (entière ou échantillonnée)."""
    windows = plan_windows(candidate.duration_s)
    if not windows:
        return []
    results: list[DownloadResult] = []
    for w in windows:
        try:
            if w is None:
                results.append(download(candidate, dest_dir))
            else:
                results.append(download(candidate, dest_dir, w[0], w[1]))
        except Exception as exc:  # noqa: BLE001 - une fenêtre ratée n'annule pas les autres
            log.error("Échec fenêtre %s de %s : %s", w, candidate.uid, exc)
    return results


if __name__ == "__main__":  # python -m src.downloader.download <url> [start end]
    import sys

    from src.models import Platform

    if len(sys.argv) < 2:
        print("usage: python -m src.downloader.download <url> [start end]")
        raise SystemExit(2)

    url = sys.argv[1]
    plat = (
        Platform.YOUTUBE if ("youtube" in url or "youtu.be" in url)
        else Platform.TWITCH if "twitch" in url
        else Platform.KICK
    )
    cand = VideoCandidate(platform=plat, source_id="test", url=url, title="test", creator="test")

    if len(sys.argv) >= 4:
        print(download(cand, settings.DOWNLOAD_DIR, float(sys.argv[2]), float(sys.argv[3])))
    else:
        for res in download_planned(cand, settings.DOWNLOAD_DIR):
            print(res)
