"""Récupération de vidéos virales Reddit via l'API OAuth officielle.

Pourquoi OAuth : Reddit renvoie 403/429 sur le JSON/RSS anonyme depuis une IP
datacenter (CI). L'API OAuth (token app-only `client_credentials`) est le moyen
fiable et supporté de lire les listings publics (~100 req/min).

La vidéo est téléchargée depuis le CDN v.redd.it via son flux HLS (audio inclus)
avec FFmpeg — aucun blocage, contrairement à l'API.

Config requise : REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (app "script" gratuite
créée sur https://www.reddit.com/prefs/apps).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from config import settings
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("sourcing.reddit")

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API = "https://oauth.reddit.com"


class RedditError(RuntimeError):
    pass


@dataclass
class RedditPost:
    id: str
    title: str
    subreddit: str
    ups: int
    permalink: str
    hls_url: str | None = None
    fallback_url: str | None = None
    duration: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"reddit:{self.id}"


@retry(exceptions=(requests.RequestException,), attempts=3)
def _get_token() -> str:
    if not (settings.REDDIT_CLIENT_ID and settings.REDDIT_CLIENT_SECRET):
        raise settings.ConfigError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET manquant")
    r = requests.post(
        _TOKEN_URL,
        auth=(settings.REDDIT_CLIENT_ID, settings.REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": settings.REDDIT_USER_AGENT},
        timeout=settings.HTTP_TIMEOUT,
    )
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RedditError("Reddit: pas d'access_token")
    return tok


def _headers(token: str) -> dict:
    return {"Authorization": f"bearer {token}", "User-Agent": settings.REDDIT_USER_AGENT}


def top_videos(
    subreddits: list[str] | None = None, time: str | None = None, limit: int = 40
) -> list[RedditPost]:
    """Meilleures vidéos (triées par upvotes) des subreddits, sur la fenêtre `time`."""
    subs = subreddits or settings.REDDIT_SUBREDDITS
    time = time or settings.REDDIT_TIME
    token = _get_token()
    posts: dict[str, RedditPost] = {}

    for sub in subs:
        try:
            r = requests.get(
                f"{_API}/r/{sub}/top",
                headers=_headers(token),
                params={"t": time, "limit": limit, "raw_json": 1},
                timeout=settings.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            children = r.json().get("data", {}).get("children", [])
        except Exception as exc:  # noqa: BLE001 - un sub KO n'annule pas les autres
            log.warning("Reddit: /r/%s indisponible : %s", sub, exc)
            continue

        for c in children:
            d = c.get("data", {})
            if not d.get("is_video") or d.get("over_18"):
                continue
            rv = (d.get("media") or {}).get("reddit_video") or {}
            hls = rv.get("hls_url")
            fb = rv.get("fallback_url")
            if not (hls or fb):
                continue
            if int(d.get("ups", 0)) < settings.REDDIT_MIN_UPS:
                continue
            pid = d.get("id", "")
            posts[pid] = RedditPost(
                id=pid,
                title=d.get("title", "") or "",
                subreddit=sub,
                ups=int(d.get("ups", 0)),
                permalink="https://www.reddit.com" + d.get("permalink", ""),
                hls_url=hls,
                fallback_url=fb,
                duration=rv.get("duration"),
                extra={"aspect": (rv.get("width", 0), rv.get("height", 0))},
            )

    result = sorted(posts.values(), key=lambda p: p.ups, reverse=True)
    log.info("Reddit: %d vidéos candidates (%d subreddits)", len(result), len(subs))
    return result


def download(post: RedditPost, dest_dir: Path) -> Path:
    """Télécharge la vidéo (HLS, audio inclus) via FFmpeg. Retourne le mp4 local."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"reddit_{post.id}.mp4"
    src = post.hls_url or post.fallback_url
    if not src:
        raise RedditError(f"{post.uid}: pas d'URL vidéo")

    # HLS -> mp4 : copie directe (rapide) ; -bsf aac_adtstoasc pour l'audio HLS.
    copy_cmd = [
        settings.FFMPEG_BIN, "-y", "-loglevel", "error",
        "-i", src, "-t", str(settings.REDDIT_CLIP_MAX_S),
        "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.run(copy_cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 10_000:
        # repli : ré-encodage (gère les cas où la copie directe échoue)
        enc_cmd = [
            settings.FFMPEG_BIN, "-y", "-loglevel", "error",
            "-i", src, "-t", str(settings.REDDIT_CLIP_MAX_S),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
        ]
        proc = subprocess.run(enc_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RedditError(f"{post.uid}: téléchargement KO : {(proc.stderr or '')[-300:]}")
    return out


if __name__ == "__main__":  # python -m src.sourcing.reddit
    for p in top_videos()[:10]:
        print(f"[{p.ups:>6}] r/{p.subreddit} | {p.title[:50]} | {p.uid}")
