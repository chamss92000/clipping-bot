"""Détection Twitch — top streamers puis leurs VODs récentes (archives).

Pourquoi VODs et pas streams live : on ne peut clipper qu'un contenu
téléchargeable. On identifie donc les streamers qui *cartonnent maintenant*
(top live streams par viewers) puis on récupère leurs dernières VODs
(`type=archive`), qui sont les rediffusions de leurs streams.

Auth : OAuth2 client-credentials (app access token). Pas de compte utilisateur
requis, le token est mis en cache en mémoire jusqu'à ~1 min avant expiration.

On utilise `requests` sur l'API Helix plutôt que `twitchio` : twitchio est
orienté chat IRC/EventSub temps réel, inadapté à un simple polling REST.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import requests

from config import settings
from src.models import Platform, VideoCandidate
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("detection.twitch")

_HELIX = "https://api.twitch.tv/helix"
_OAUTH = "https://id.twitch.tv/oauth2/token"

# Cache token en mémoire : (access_token, expiry_epoch)
_token_cache: tuple[str, float] | None = None


class TwitchError(RuntimeError):
    pass


@retry(exceptions=(requests.RequestException,))
def _fetch_app_token() -> tuple[str, float]:
    resp = requests.post(
        _OAUTH,
        params={
            "client_id": settings.TWITCH_CLIENT_ID,
            "client_secret": settings.TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=settings.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    expiry = time.time() + int(data.get("expires_in", 3600))
    return data["access_token"], expiry


def _get_token() -> str:
    global _token_cache
    now = time.time()
    if _token_cache and _token_cache[1] - 60 > now:
        return _token_cache[0]
    if not (settings.TWITCH_CLIENT_ID and settings.TWITCH_CLIENT_SECRET):
        raise settings.ConfigError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET manquant")
    token, expiry = _fetch_app_token()
    _token_cache = (token, expiry)
    log.debug("Twitch: nouveau app access token obtenu")
    return token


def _headers() -> dict[str, str]:
    return {
        "Client-ID": settings.TWITCH_CLIENT_ID or "",
        "Authorization": f"Bearer {_get_token()}",
    }


@retry(exceptions=(requests.RequestException,))
def _helix_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{_HELIX}/{path}",
        headers=_headers(),
        params=params,
        timeout=settings.HTTP_TIMEOUT,
    )
    # 401 => token périmé/révoqué : on purge le cache et on laisse le retry rejouer.
    if resp.status_code == 401:
        global _token_cache
        _token_cache = None
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()


def _top_streams(min_viewers: int, limit: int, languages: list[str]) -> list[dict]:
    """Streams live triés par viewers (Helix les renvoie déjà décroissants)."""
    collected: list[dict] = []
    cursor: str | None = None
    langs = set(languages)
    # On pagine jusqu'à avoir assez de streams au-dessus du seuil.
    while len(collected) < limit:
        params = {"first": 100}
        if cursor:
            params["after"] = cursor
        data = _helix_get("streams", params)
        page = data.get("data", [])
        if not page:
            break
        for s in page:
            if s.get("viewer_count", 0) < min_viewers:
                # liste triée décroissante => tout ce qui suit est sous le seuil
                return collected
            if langs and s.get("language") not in langs:
                continue
            collected.append(s)
            if len(collected) >= limit:
                break
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor:
            break
    return collected


def _recent_clips(user_id: str, count: int, started_at: str) -> list[dict]:
    """Top clips (triés par vues) d'un streamer depuis `started_at` (RFC3339)."""
    data = _helix_get(
        "clips",
        {"broadcaster_id": user_id, "first": min(count, 100), "started_at": started_at},
    )
    return data.get("data", [])


def _clip_trend_score(views: int, created_at: str | None) -> float:
    """Score 0-100 basé sur la vélocité (vues/heure) du clip."""
    age_h = 24.0
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_h = max(1.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        except ValueError:
            pass
    vph = views / age_h
    return round(min(100.0, 20.0 * math.log10(vph + 1.0) + math.log10(max(views, 1))), 2)


def _clip_to_candidate(clip: dict) -> VideoCandidate | None:
    cid = clip.get("id")
    url = clip.get("url")
    if not cid or not url:
        return None
    views = int(clip.get("view_count", 0))
    duration = int(round(float(clip.get("duration", 0) or 0)))
    return VideoCandidate(
        platform=Platform.TWITCH,
        source_id=str(cid),
        url=url,
        title=clip.get("title", "") or "",
        creator=clip.get("broadcaster_name", "") or "",
        views=views,
        duration_s=duration or None,
        published_at=clip.get("created_at"),
        thumbnail=clip.get("thumbnail_url"),
        score=_clip_trend_score(views, clip.get("created_at")),
        extra={
            "is_clip": True,  # <-- le clip EST déjà le moment viral
            "language": clip.get("language"),
            "game_id": clip.get("game_id"),
            "creator_name": clip.get("creator_name"),
        },
    )


def detect_clips(
    min_viewers: int | None = None,
    top_streams: int | None = None,
    clips_per: int | None = None,
    languages: list[str] | None = None,
) -> list[VideoCandidate]:
    """Détecte les clips Twitch DÉJÀ viraux (API Clips) chez les streamers chauds.

    Bien supérieur au clipping de VODs : chaque clip est un moment sélectionné
    par la communauté, court, téléchargeable, et filtrable par langue.
    """
    min_viewers = min_viewers if min_viewers is not None else settings.TWITCH_MIN_VIEWERS
    top_streams = top_streams or settings.TWITCH_TOP_STREAMS
    clips_per = clips_per or settings.TWITCH_CLIPS_PER_STREAMER
    languages = languages if languages is not None else settings.TWITCH_LANGUAGES
    langs = set(languages)

    try:
        streams = _top_streams(min_viewers, top_streams, languages)
    except Exception as exc:  # noqa: BLE001
        log.error("Twitch: échec récupération top streams : %s", exc)
        return []
    log.info("Twitch: %d streamers chauds, recherche de leurs clips…", len(streams))

    started_at = (
        datetime.now(timezone.utc) - timedelta(days=settings.TWITCH_CLIPS_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates: dict[str, VideoCandidate] = {}
    for stream in streams:
        uid = stream.get("user_id")
        if not uid:
            continue
        try:
            clips = _recent_clips(uid, clips_per, started_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("Twitch: clips indisponibles pour %s : %s", stream.get("user_name"), exc)
            continue
        for clip in clips:
            if langs and clip.get("language") not in langs:
                continue
            if int(clip.get("view_count", 0)) < settings.TWITCH_CLIP_MIN_VIEWS:
                continue
            cand = _clip_to_candidate(clip)
            if cand and cand.uid not in candidates:
                candidates[cand.uid] = cand

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    log.info("Twitch: %d clips viraux candidats", len(result))
    return result


def _recent_vods(user_id: str, count: int) -> list[dict]:
    data = _helix_get(
        "videos",
        {"user_id": user_id, "type": "archive", "sort": "time", "first": min(count, 100)},
    )
    return data.get("data", [])


def _parse_twitch_duration(s: str) -> int:
    """'1h2m3s' / '45m10s' / '30s' -> secondes."""
    total, num = 0, ""
    units = {"h": 3600, "m": 60, "s": 1}
    for ch in s:
        if ch.isdigit():
            num += ch
        elif ch in units and num:
            total += int(num) * units[ch]
            num = ""
    return total


def _vod_to_candidate(vod: dict, stream_ctx: dict) -> VideoCandidate | None:
    duration = _parse_twitch_duration(vod.get("duration", ""))
    if duration < settings.TWITCH_MIN_VOD_DURATION_S:
        return None
    views = int(vod.get("view_count", 0))
    live_viewers = int(stream_ctx.get("viewer_count", 0))
    # Score de tendance 0-100, MÊME ÉCHELLE que YouTube (sinon une plateforme
    # gagne toujours) : l'audience live actuelle est le signal de "chaud".
    score = min(100.0, 20.0 * math.log10(live_viewers + 1.0))
    score += min(6.0, math.log10(max(views, 1)))  # petit bonus notoriété du VOD
    score = round(min(100.0, score), 2)

    return VideoCandidate(
        platform=Platform.TWITCH,
        source_id=str(vod.get("id")),
        url=vod.get("url", f"https://www.twitch.tv/videos/{vod.get('id')}"),
        title=vod.get("title", ""),
        creator=vod.get("user_name", stream_ctx.get("user_name", "")),
        views=views,
        duration_s=duration,
        published_at=vod.get("created_at") or vod.get("published_at"),
        thumbnail=(vod.get("thumbnail_url") or "").replace("%{width}", "640").replace("%{height}", "360"),
        score=score,
        extra={
            "user_id": vod.get("user_id"),
            "game_name": stream_ctx.get("game_name"),
            "game_id": stream_ctx.get("game_id"),
            "live_viewer_count": live_viewers,
            "language": stream_ctx.get("language"),
        },
    )


def detect(
    min_viewers: int | None = None,
    top_streams: int | None = None,
    vods_per_streamer: int | None = None,
    languages: list[str] | None = None,
) -> list[VideoCandidate]:
    """Point d'entrée Twitch. Par défaut => clips déjà viraux (bien meilleur).
    `TWITCH_SOURCE=vods` bascule sur l'ancien échantillonnage de VODs."""
    if settings.TWITCH_SOURCE == "clips":
        return detect_clips(min_viewers, top_streams, None, languages)
    return _detect_vods(min_viewers, top_streams, vods_per_streamer, languages)


def _detect_vods(
    min_viewers: int | None = None,
    top_streams: int | None = None,
    vods_per_streamer: int | None = None,
    languages: list[str] | None = None,
) -> list[VideoCandidate]:
    """Ancien mode : VODs candidates issues des streamers les plus regardés."""
    min_viewers = min_viewers if min_viewers is not None else settings.TWITCH_MIN_VIEWERS
    top_streams = top_streams or settings.TWITCH_TOP_STREAMS
    vods_per_streamer = vods_per_streamer or settings.TWITCH_VODS_PER_STREAMER
    languages = languages if languages is not None else settings.TWITCH_LANGUAGES

    try:
        streams = _top_streams(min_viewers, top_streams, languages)
    except Exception as exc:  # noqa: BLE001
        log.error("Twitch: échec récupération top streams : %s", exc)
        return []

    log.info("Twitch: %d streams live au-dessus de %d viewers", len(streams), min_viewers)

    candidates: dict[str, VideoCandidate] = {}
    for stream in streams:
        user_id = stream.get("user_id")
        if not user_id:
            continue
        try:
            vods = _recent_vods(user_id, vods_per_streamer)
        except Exception as exc:  # noqa: BLE001
            log.warning("Twitch: VODs indisponibles pour %s : %s", stream.get("user_name"), exc)
            continue
        for vod in vods:
            cand = _vod_to_candidate(vod, stream)
            if cand and cand.uid not in candidates:
                candidates[cand.uid] = cand

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    log.info("Twitch: %d VODs candidates", len(result))
    return result


if __name__ == "__main__":  # python -m src.detection.twitch
    settings.validate(["twitch"])
    for c in detect()[:10]:
        lv = c.extra.get("live_viewer_count")
        print(f"[{c.score:6.2f}] live={lv} | {c.creator} — {c.title[:60]}")
        print(f"          {c.url}  ({c.duration_s}s, game={c.extra.get('game_name')})")
