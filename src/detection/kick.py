"""Détection Kick — API publique non officielle (best-effort).

⚠️ Kick n'a pas d'API publique officielle stable et son edge est protégé par
Cloudflare : les endpoints peuvent renvoyer 403 selon l'IP du runner. Ce module
est donc *tolérant à la panne* — s'il est bloqué, il log un warning et renvoie
une liste vide sans jamais casser le pipeline (YouTube/Twitch prennent le relais).

Stratégie :
  1. Endpoint "featured livestreams" (si accessible) pour découvrir des chaînes
     chaudes automatiquement.
  2. Fallback / complément : liste de slugs configurée (`KICK_CHANNELS`).
  3. Pour chaque chaîne live au-dessus du seuil de viewers, on récupère ses
     VODs récentes via `/api/v2/channels/{slug}/videos`.

Endpoints utilisés (non officiels, susceptibles de changer) :
  * GET https://kick.com/api/v2/channels/{slug}
  * GET https://kick.com/api/v2/channels/{slug}/videos
  * GET https://kick.com/stream/featured-livestreams/{lang}   (best-effort)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import requests

from config import settings
from src.models import Platform, VideoCandidate
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("detection.kick")

_BASE = "https://kick.com"

# En-têtes façon navigateur : améliore les chances de passer Cloudflare.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Referer": "https://kick.com/",
}


class KickBlocked(RuntimeError):
    """Levée quand Kick renvoie un blocage (403/Cloudflare)."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    return s


@retry(exceptions=(requests.RequestException,), attempts=2)
def _get_json(sess: requests.Session, path: str) -> dict | list | None:
    resp = sess.get(f"{_BASE}{path}", timeout=settings.HTTP_TIMEOUT)
    if resp.status_code in (403, 429, 503):
        raise KickBlocked(f"Kick a renvoyé {resp.status_code} sur {path}")
    resp.raise_for_status()
    if "application/json" not in resp.headers.get("Content-Type", ""):
        # Cloudflare renvoie parfois du HTML de challenge avec un 200.
        raise KickBlocked(f"Réponse non-JSON sur {path} (probable challenge Cloudflare)")
    return resp.json()


def _discover_featured(sess: requests.Session, limit: int) -> list[str]:
    """Tente de découvrir des slugs de chaînes en vedette. Best-effort."""
    slugs: list[str] = []
    for lang in ("en", "fr"):
        try:
            data = _get_json(sess, f"/stream/featured-livestreams/{lang}")
        except Exception as exc:  # noqa: BLE001
            log.debug("Kick: featured (%s) indisponible : %s", lang, exc)
            continue
        items = data if isinstance(data, list) else (data or {}).get("data", [])
        for it in items or []:
            slug = (
                (it.get("channel") or {}).get("slug")
                or it.get("slug")
                or (it.get("user") or {}).get("username")
            )
            if slug and slug not in slugs:
                slugs.append(slug)
            if len(slugs) >= limit:
                return slugs
    return slugs


def _parse_duration_ms(value) -> int | None:
    """Kick renvoie parfois la durée en ms (int) ou en secondes. On normalise en s."""
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    # Heuristique : > 24h en "secondes" est improbable pour un VOD => c'était des ms.
    return v // 1000 if v > 86_400 else v


def _channel_info(sess: requests.Session, slug: str) -> dict | None:
    try:
        return _get_json(sess, f"/api/v2/channels/{slug}")  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        log.warning("Kick: infos chaîne '%s' indisponibles : %s", slug, exc)
        return None


def _channel_vods(sess: requests.Session, slug: str, count: int) -> list[dict]:
    try:
        data = _get_json(sess, f"/api/v2/channels/{slug}/videos")
    except Exception as exc:  # noqa: BLE001
        log.warning("Kick: VODs de '%s' indisponibles : %s", slug, exc)
        return []
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return (items or [])[:count]


def _vod_to_candidate(vod: dict, channel: dict, live_viewers: int) -> VideoCandidate | None:
    uuid = vod.get("uuid") or (vod.get("video") or {}).get("uuid")
    if not uuid:
        return None
    duration = _parse_duration_ms(vod.get("duration") or (vod.get("livestream") or {}).get("duration"))
    if duration is not None and duration < 60:
        return None
    views = int(vod.get("views") or vod.get("view_count") or 0)
    title = (
        vod.get("session_title")
        or vod.get("title")
        or (vod.get("livestream") or {}).get("session_title")
        or ""
    )
    creator = (channel.get("user") or {}).get("username") or channel.get("slug", "")
    # Même échelle 0-100 que YouTube/Twitch (comparaison inter-plateformes juste).
    score = min(100.0, 20.0 * math.log10(live_viewers + 1.0))
    score += min(6.0, math.log10(max(views, 1)))
    score = round(min(100.0, score), 2)

    return VideoCandidate(
        platform=Platform.KICK,
        source_id=str(uuid),
        url=f"{_BASE}/video/{uuid}",
        title=title,
        creator=creator,
        views=views,
        duration_s=duration,
        published_at=vod.get("created_at") or vod.get("start_time"),
        thumbnail=(vod.get("thumbnail") or {}).get("src") if isinstance(vod.get("thumbnail"), dict) else vod.get("thumbnail"),
        score=score,
        extra={
            "slug": channel.get("slug"),
            "live_viewer_count": live_viewers,
            "categories": [c.get("name") for c in (vod.get("categories") or []) if isinstance(c, dict)],
        },
    )


def _clip_trend_score(views: int, created_at: str | None) -> float:
    age_h = 24.0
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_h = max(1.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        except ValueError:
            pass
    vph = views / age_h
    return round(min(100.0, 20.0 * math.log10(vph + 1.0) + math.log10(max(views, 1))), 2)


def _channel_clips(sess: requests.Session, slug: str, count: int) -> list[dict]:
    try:
        data = _get_json(sess, f"/api/v2/channels/{slug}/clips?sort=view&time={settings.KICK_CLIPS_TIME}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Kick: clips de '%s' indisponibles : %s", slug, exc)
        return []
    items = data.get("clips") if isinstance(data, dict) else data
    items = items or (data.get("data") if isinstance(data, dict) else []) or []
    return items[:count]


def detect_clips(channels: list[str] | None = None) -> list[VideoCandidate]:
    """Clips Kick DÉJÀ viraux (triés par vues). Best-effort, [] si bloqué."""
    channels = list(channels or settings.KICK_CHANNELS)
    # On inclut les chaînes FR connues pour alimenter le marché francophone.
    for slug in settings.KICK_FR_CHANNELS:
        if slug not in channels:
            channels.append(slug)
    sess = _session()
    discovered = _discover_featured(sess, settings.KICK_FEATURED_LIMIT)
    for slug in discovered:
        if slug not in channels:
            channels.append(slug)

    candidates: dict[str, VideoCandidate] = {}
    for slug in channels:
        for clip in _channel_clips(sess, slug, settings.KICK_CLIPS_PER_CHANNEL):
            cid = clip.get("id")
            if not cid:
                continue
            views = int(clip.get("view_count") or clip.get("views") or 0)
            if views < settings.KICK_CLIP_MIN_VIEWS:
                continue
            dur = clip.get("duration")
            dur = int(round(float(dur))) if dur else None
            cand = VideoCandidate(
                platform=Platform.KICK,
                source_id=str(cid),
                url=f"{_BASE}/{slug}/clips/{cid}",
                title=clip.get("title", "") or "",
                creator=((clip.get("channel") or {}).get("slug")) or slug,
                views=views,
                duration_s=dur,
                published_at=clip.get("created_at"),
                thumbnail=clip.get("thumbnail_url"),
                score=_clip_trend_score(views, clip.get("created_at")),
                extra={"is_clip": True, "slug": slug,
                       "category": (clip.get("category") or {}).get("name")},
            )
            if cand.uid not in candidates:
                candidates[cand.uid] = cand

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    log.info("Kick: %d clips viraux candidats", len(result))
    return result


def detect(
    channels: list[str] | None = None,
    min_viewers: int | None = None,
    vods_per_channel: int | None = None,
) -> list[VideoCandidate]:
    """Point d'entrée Kick. Par défaut => clips déjà viraux."""
    if settings.KICK_SOURCE == "clips":
        return detect_clips(channels)
    return _detect_vods(channels, min_viewers, vods_per_channel)


def _detect_vods(
    channels: list[str] | None = None,
    min_viewers: int | None = None,
    vods_per_channel: int | None = None,
) -> list[VideoCandidate]:
    """Ancien mode : VODs candidates Kick. Renvoie [] si Kick est inaccessible."""
    channels = list(channels or settings.KICK_CHANNELS)
    min_viewers = min_viewers if min_viewers is not None else settings.KICK_MIN_VIEWERS
    vods_per_channel = vods_per_channel or settings.KICK_VODS_PER_CHANNEL

    sess = _session()

    # 1) Découverte automatique (best-effort), fusionnée avec la liste configurée.
    discovered = _discover_featured(sess, settings.KICK_FEATURED_LIMIT)
    if discovered:
        log.info("Kick: %d chaînes découvertes en featured", len(discovered))
    for slug in discovered:
        if slug not in channels:
            channels.append(slug)

    candidates: dict[str, VideoCandidate] = {}
    blocked_count = 0

    for slug in channels:
        channel = _channel_info(sess, slug)
        if channel is None:
            blocked_count += 1
            continue
        livestream = channel.get("livestream") or {}
        live_viewers = int(livestream.get("viewer_count", 0)) if livestream else 0
        # Si la chaîne est offline, on garde quand même ses VODs récentes
        # (contenu populaire), mais on ne filtre le seuil que si elle est live.
        if livestream and live_viewers < min_viewers:
            log.debug("Kick: '%s' live sous le seuil (%d viewers)", slug, live_viewers)
            continue
        for vod in _channel_vods(sess, slug, vods_per_channel):
            cand = _vod_to_candidate(vod, channel, live_viewers)
            if cand and cand.uid not in candidates:
                candidates[cand.uid] = cand

    if blocked_count and not candidates:
        log.warning(
            "Kick: aucune donnée récupérée (%d chaînes inaccessibles — "
            "probable blocage Cloudflare). Pipeline poursuivi sans Kick.",
            blocked_count,
        )

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    log.info("Kick: %d VODs candidates", len(result))
    return result


if __name__ == "__main__":  # python -m src.detection.kick
    for c in detect()[:10]:
        print(f"[{c.score:6.2f}] {c.views:>8} vues | {c.creator} — {c.title[:60]}")
        print(f"          {c.url}  ({c.duration_s}s)")
