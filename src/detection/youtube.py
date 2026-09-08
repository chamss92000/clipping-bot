"""Détection YouTube — vidéos trending par catégorie via YouTube Data API v3.

Stratégie :
  * `videos.list(chart="mostPopular")` par catégorie et région : 1 seul appel
    quota par catégorie (coût quota = 1 unité), renvoie déjà snippet +
    statistics + contentDetails => pas d'appel supplémentaire nécessaire.
  * On filtre par durée (on écarte les Shorts déjà verticaux et les vidéos
    trop longues) et par nombre de vues minimal.
  * Score de tri = log(vues) pondéré par la fraîcheur (récence).

Quota : YouTube Data API = 10 000 unités/jour. `videos.list` = 1 unité/appel.
Avec ~3 catégories x 4 cycles/jour = 12 unités/jour. Très large marge.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from src.models import Platform, VideoCandidate
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("detection.youtube")

# Durée ISO8601 (PT#H#M#S) -> secondes
_ISO_DURATION = re.compile(
    r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?"
)


def _parse_iso_duration(iso: str) -> int:
    m = _ISO_DURATION.fullmatch(iso or "")
    if not m:
        return 0
    h = int(m.group("h") or 0)
    mi = int(m.group("m") or 0)
    s = int(m.group("s") or 0)
    return h * 3600 + mi * 60 + s


def _age_hours(published_at: str | None) -> float | None:
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(1.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)


def _trend_score(views: int, published_at: str | None, likes: int | None, comments: int | None) -> float:
    """Score de tendance 0-100, comparable entre plateformes.

    Le vrai signal de "tendance" n'est pas le nombre de vues brut mais la
    **vélocité** (vues/heure) : une vidéo à 100k vues en 3h explose bien plus
    qu'une à 1M vues en 3 semaines. On y ajoute un petit bonus d'engagement
    (taux de likes), signe d'un contenu qui fait réagir.
    """
    age_h = _age_hours(published_at) or 72.0
    velocity = views / age_h                      # vues par heure
    score = min(100.0, 20.0 * math.log10(velocity + 1.0))
    if likes and views:
        score += min(8.0, (likes / views) * 200.0)   # taux de likes (~2-6% => +4 à +8)
    if comments and views:
        score += min(4.0, (comments / views) * 800.0)
    return round(min(100.0, score), 2)


def _build_client():
    """Construit le client YouTube (cache_discovery=False => pas d'écriture disque)."""
    if not settings.YOUTUBE_API_KEY:
        raise settings.ConfigError("YOUTUBE_API_KEY manquant")
    return build(
        "youtube",
        "v3",
        developerKey=settings.YOUTUBE_API_KEY,
        cache_discovery=False,
    )


@retry(exceptions=(HttpError, ConnectionError, TimeoutError))
def _list_most_popular(client, category_id: str, region: str, max_results: int) -> list[dict]:
    resp = (
        client.videos()
        .list(
            part="snippet,statistics,contentDetails",
            chart="mostPopular",
            regionCode=region,
            videoCategoryId=category_id,
            maxResults=min(max_results, 50),
        )
        .execute()
    )
    return resp.get("items", [])


def _to_candidate(item: dict) -> VideoCandidate | None:
    try:
        vid = item["id"]
        snippet = item["snippet"]
        stats = item.get("statistics", {})
        content = item.get("contentDetails", {})
    except KeyError:
        return None

    duration = _parse_iso_duration(content.get("duration", ""))
    views = int(stats.get("viewCount", 0))

    if duration < settings.YOUTUBE_MIN_DURATION_S or duration > settings.YOUTUBE_MAX_DURATION_S:
        return None
    if views < settings.YOUTUBE_MIN_VIEWS:
        return None

    published_at = snippet.get("publishedAt")
    likes = int(stats.get("likeCount", 0)) if stats.get("likeCount") else None
    comments = int(stats.get("commentCount", 0)) if stats.get("commentCount") else None
    score = _trend_score(views, published_at, likes, comments)

    thumbs = snippet.get("thumbnails", {})
    thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default") or {}).get("url")

    return VideoCandidate(
        platform=Platform.YOUTUBE,
        source_id=vid,
        url=f"https://www.youtube.com/watch?v={vid}",
        title=snippet.get("title", ""),
        creator=snippet.get("channelTitle", ""),
        views=views,
        duration_s=duration,
        published_at=published_at,
        thumbnail=thumb,
        score=round(score, 4),
        extra={
            "channel_id": snippet.get("channelId"),
            "category_id": snippet.get("categoryId"),
            "like_count": likes,
            "comment_count": comments,
            #: vues/heure — c'est LE signal de tendance, affiché dans le rapport.
            "views_per_hour": round(views / (_age_hours(published_at) or 72.0)),
            "age_hours": round(_age_hours(published_at) or 0),
            "tags": snippet.get("tags", []),
        },
    )


def detect(
    region: str | None = None,
    category_ids: list[str] | None = None,
    max_results: int | None = None,
) -> list[VideoCandidate]:
    """Retourne les vidéos trending YouTube filtrées, triées par score décroissant.

    Ne lève jamais si une catégorie échoue : on log et on continue (robustesse).
    Peut lever `ConfigError` uniquement si la clé API est absente.
    """
    region = region or settings.YOUTUBE_REGION
    category_ids = category_ids or settings.YOUTUBE_CATEGORY_IDS
    max_results = max_results or settings.YOUTUBE_MAX_RESULTS

    client = _build_client()
    candidates: dict[str, VideoCandidate] = {}

    for cat in category_ids:
        try:
            items = _list_most_popular(client, cat, region, max_results)
        except Exception as exc:  # noqa: BLE001 - on isole la panne d'une catégorie
            log.error("YouTube: échec catégorie %s (%s) : %s", cat, region, exc)
            continue

        kept = 0
        for item in items:
            cand = _to_candidate(item)
            if cand and cand.uid not in candidates:
                candidates[cand.uid] = cand
                kept += 1
        log.info("YouTube: catégorie %s => %d/%d vidéos retenues", cat, kept, len(items))

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    log.info("YouTube: %d candidats uniques au total", len(result))
    return result


if __name__ == "__main__":  # python -m src.detection.youtube
    settings.validate(["youtube"])
    for c in detect()[:10]:
        print(f"[{c.score:6.2f}] {c.views:>10,} vues | {c.creator} — {c.title[:70]}")
        print(f"          {c.url}  ({c.duration_s}s)")
