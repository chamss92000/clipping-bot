"""Détection Kick — clips DÉJÀ viraux des jeux suivis (best-effort).

⚠️ Kick n'a pas d'API publique officielle stable et son edge est protégé par
Cloudflare : les endpoints peuvent renvoyer 403 selon l'IP du runner. Ce module
est donc *tolérant à la panne* — s'il est bloqué, il log un warning et renvoie
une liste vide sans jamais casser le pipeline (Twitch prend le relais).

Stratégie : chaînes configurées (+ découverte "featured" best-effort), puis
leurs clips les plus vus, en ne gardant QUE ceux dont la catégorie est un jeu
suivi (GTA 6 en priorité).

Endpoints utilisés (non officiels, susceptibles de changer) :
  * GET https://kick.com/api/v2/channels/{slug}/clips?sort=view&time=…
  * GET https://kick.com/stream/featured-livestreams/{lang}   (best-effort)
"""

from __future__ import annotations

import requests

from config import settings
from src.models import Platform, VideoCandidate
from src.utils.logging import get_logger
from src.utils.retry import retry

from .games import match_game
from .twitch import clip_trend_score

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


def _channel_clips(sess: requests.Session, slug: str, count: int) -> list[dict]:
    try:
        data = _get_json(sess, f"/api/v2/channels/{slug}/clips?sort=view&time={settings.KICK_CLIPS_TIME}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Kick: clips de '%s' indisponibles : %s", slug, exc)
        return []
    items = data.get("clips") if isinstance(data, dict) else data
    items = items or (data.get("data") if isinstance(data, dict) else []) or []
    return items[:count]


def detect() -> list[VideoCandidate]:
    """Clips Kick viraux sur les jeux suivis. Best-effort, [] si bloqué."""
    if not settings.KICK_ENABLE:
        return []
    targets = [t.lower() for t in settings.TARGET_CHANNELS_KICK]
    sess = _session()
    if settings.TARGET_ONLY:
        channels = list(targets)
    else:
        channels = list(settings.KICK_CHANNELS)
        for slug in settings.KICK_FR_CHANNELS + targets:
            if slug not in channels:
                channels.append(slug)
        for slug in _discover_featured(sess, settings.KICK_FEATURED_LIMIT):
            if slug not in channels:
                channels.append(slug)

    fr_set = {s.lower() for s in settings.KICK_FR_CHANNELS}
    candidates: dict[str, VideoCandidate] = {}
    off_topic = 0
    for slug in channels:
        for clip in _channel_clips(sess, slug, settings.KICK_CLIPS_PER_CHANNEL):
            cid = clip.get("id")
            if not cid:
                continue
            views = int(clip.get("view_count") or clip.get("views") or 0)
            if views < settings.KICK_CLIP_MIN_VIEWS:
                continue
            is_target = slug.lower() in targets
            cat = (clip.get("category") or {}).get("name") or ""
            game = match_game(cat)
            if not game and not (is_target and settings.TARGET_ANY_GAME):
                off_topic += 1
                continue
            dur = clip.get("duration")
            dur = int(round(float(dur))) if dur else None
            if dur is not None and dur < settings.SOURCE_CLIP_MIN_S:
                continue
            score = clip_trend_score(views, clip.get("created_at"))
            if game and game.primary:
                score += settings.PRIMARY_GAME_BOOST
            if is_target:
                score += settings.TARGET_SCORE_BOOST
            creator = ((clip.get("channel") or {}).get("slug")) or slug
            cand = VideoCandidate(
                platform=Platform.KICK,
                source_id=str(cid),
                url=f"{_BASE}/{slug}/clips/{cid}",
                title=clip.get("title", "") or "",
                creator=creator,
                views=views,
                duration_s=dur,
                published_at=clip.get("created_at"),
                thumbnail=clip.get("thumbnail_url"),
                score=round(score, 2),
                extra={
                    "is_clip": True, "slug": slug, "target": is_target,
                    "game_name": cat, "primary_game": bool(game and game.primary),
                    # Kick ne donne pas la langue : connue seulement pour les
                    # chaînes FR listées (sinon Whisper la détecte).
                    "language": "fr" if slug.lower() in fr_set else None,
                },
            )
            candidates.setdefault(cand.uid, cand)

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    if off_topic:
        log.info("Kick: %d clip(s) hors jeux suivis écarté(s)", off_topic)
    log.info("Kick: %d clips candidats", len(result))
    return result


if __name__ == "__main__":  # python -m src.detection.kick
    for c in detect()[:10]:
        print(f"[{c.score:6.2f}] {c.views:>8} vues | {c.extra.get('game_name')} | {c.creator} — {c.title[:60]}")
        print(f"          {c.url}  ({c.duration_s}s)")
