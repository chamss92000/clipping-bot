"""Récupération de vidéos "cute/animaux/satisfying" via l'API Pexels (gratuite).

Alternative fiable à Reddit (dont l'API est désormais verrouillée pour les
nouvelles apps). Pexels = libre de droits (usage commercial OK, sans attribution
obligatoire), pas de blocage en CI. Réutilise PEXELS_API_KEY (déjà configurée).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import requests

from config import settings
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("sourcing.pexels")

_SEARCH = "https://api.pexels.com/videos/search"


class PexelsError(RuntimeError):
    pass


@dataclass
class PexelsClip:
    id: str
    title: str
    query: str
    url: str
    link: str            # URL de téléchargement du fichier vidéo
    height: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"pexels:{self.id}"


def _pick_file(video: dict) -> tuple[str | None, int]:
    """Choisit le meilleur fichier : portrait de préférence, résolution raisonnable."""
    best_link, best_h, best_score = None, 0, -1
    for f in video.get("video_files", []):
        w, h = f.get("width") or 0, f.get("height") or 0
        if h < 500:
            continue
        portrait = h >= w
        # on privilégie le portrait, puis une hauteur ~1080-1920
        score = (2 if portrait else 0) + (2 if 900 <= h <= 1920 else 1)
        if score > best_score:
            best_score, best_link, best_h = score, f.get("link"), h
    return best_link, best_h


@retry(exceptions=(requests.RequestException,), attempts=3)
def _search(query: str, per_page: int) -> list[dict]:
    r = requests.get(
        _SEARCH,
        headers={"Authorization": settings.PEXELS_API_KEY},
        params={"query": query, "per_page": per_page, "orientation": "portrait", "size": "medium"},
        timeout=settings.HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("videos", [])


def top_videos(queries: list[str] | None = None, per_query: int | None = None) -> list[PexelsClip]:
    """Vidéos cute/satisfying variées (dédoublonnées) depuis Pexels."""
    if not settings.PEXELS_API_KEY:
        raise settings.ConfigError("PEXELS_API_KEY manquant")
    queries = queries or settings.CUTE_QUERIES
    per_query = per_query or settings.CUTE_PER_QUERY
    out: dict[str, PexelsClip] = {}
    for q in queries:
        try:
            vids = _search(q, per_query)
        except Exception as exc:  # noqa: BLE001 - une requête KO n'annule pas les autres
            log.warning("Pexels: requête '%s' KO : %s", q, exc)
            continue
        for v in vids:
            vid = str(v.get("id"))
            if vid in out:
                continue
            link, h = _pick_file(v)
            if not link:
                continue
            out[vid] = PexelsClip(
                id=vid, title=(v.get("user", {}).get("name") or "cute"),
                query=q, url=v.get("url", ""), link=link, height=h,
            )
    result = list(out.values())
    log.info("Pexels: %d vidéos cute candidates (%d requêtes)", len(result), len(queries))
    return result


def download(clip: PexelsClip, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"pexels_{clip.id}.mp4"
    with requests.get(clip.link, stream=True, timeout=90) as dl:
        dl.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in dl.iter_content(1 << 20):
                fh.write(chunk)
    if out.stat().st_size < 10_000:
        raise PexelsError(f"{clip.uid}: fichier vide")
    return out


if __name__ == "__main__":  # python -m src.sourcing.pexels_clips
    for c in top_videos()[:10]:
        print(f"[{c.height:>4}p] {c.query} | {c.title} | {c.uid}")
