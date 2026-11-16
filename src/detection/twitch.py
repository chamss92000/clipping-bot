"""Détection Twitch — clips DÉJÀ viraux, par JEU (focus GTA 6).

Stratégie : on interroge directement l'API Clips par catégorie de jeu
(`GET /helix/clips?game_id=…&started_at=…`). Twitch renvoie les clips triés par
vues décroissantes, TOUS streamers confondus — bien plus ciblé que l'ancienne
approche « top streamers live → leurs clips » (qui ramenait surtout du Just
Chatting et ratait les petits streamers qui font LE clip du jour).

Chaque clip est un moment sélectionné par la communauté, court (≤ 60 s),
téléchargeable en CI et filtrable par langue.

Auth : OAuth2 client-credentials (app access token), mis en cache en mémoire.
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

from .games import TrackedGame, match_game, tracked_games

log = get_logger("detection.twitch")

_HELIX = "https://api.twitch.tv/helix"
_OAUTH = "https://id.twitch.tv/oauth2/token"

# Cache token en mémoire : (access_token, expiry_epoch)
_token_cache: tuple[str, float] | None = None


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
    return token


def _headers() -> dict[str, str]:
    return {
        "Client-ID": settings.TWITCH_CLIENT_ID or "",
        "Authorization": f"Bearer {_get_token()}",
    }


@retry(exceptions=(requests.RequestException,))
def _helix_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{_HELIX}/{path}", headers=_headers(), params=params, timeout=settings.HTTP_TIMEOUT
    )
    if resp.status_code == 401:  # token périmé/révoqué : purge + retry
        global _token_cache
        _token_cache = None
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Jeux
# ---------------------------------------------------------------------------
def _resolve_games(games: list[TrackedGame]) -> dict[str, TrackedGame]:
    """{game_id: TrackedGame}. Nom exact via /games, sinon /search/categories."""
    out: dict[str, TrackedGame] = {}
    if not games:
        return out
    try:
        data = _helix_get("games", {"name": [g.info.name for g in games]})
        for row in data.get("data", []):
            g = match_game(row.get("name"))
            if g:
                out[str(row["id"])] = g
    except Exception as exc:  # noqa: BLE001
        log.warning("Twitch: résolution /games échouée : %s", exc)

    found = {g.info.name for g in out.values()}
    for g in games:
        if g.info.name in found:
            continue
        # Nom pas exact (ou catégorie renommée) : recherche puis alias.
        try:
            rows = _helix_get("search/categories", {"query": g.info.name, "first": 10}).get("data", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("Twitch: recherche de catégorie '%s' échouée : %s", g.info.name, exc)
            continue
        for row in rows:
            if match_game(row.get("name")) is g or (row.get("name") or "").lower() == g.info.name.lower():
                out[str(row["id"])] = g
                break
        else:
            log.warning("Twitch: catégorie introuvable pour '%s' (vérifie le nom exact).", g.info.name)
    return out


def _games_names(game_ids: list[str]) -> dict[str, str]:
    """{game_id: nom} pour des ids arbitraires (clips de créateurs ciblés)."""
    ids = sorted({g for g in game_ids if g})
    out: dict[str, str] = {}
    for i in range(0, len(ids), 100):
        try:
            data = _helix_get("games", {"id": ids[i : i + 100]})
        except Exception as exc:  # noqa: BLE001
            log.debug("Twitch: noms de jeux indisponibles : %s", exc)
            continue
        for g in data.get("data", []):
            out[str(g.get("id"))] = g.get("name", "")
    return out


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------
def _paged_clips(params: dict, pages: int) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max(1, pages)):
        p = dict(params, first=100)
        if cursor:
            p["after"] = cursor
        data = _helix_get("clips", p)
        out.extend(data.get("data", []))
        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return out


def clip_trend_score(views: int, created_at: str | None) -> float:
    """Score 0-100 : vélocité (vues/heure) + petit bonus volume. Commun Twitch/Kick."""
    age_h = 24.0
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_h = max(1.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        except ValueError:
            pass
    vph = views / age_h
    return round(min(100.0, 20.0 * math.log10(vph + 1.0) + math.log10(max(views, 1))), 2)


def _clip_to_candidate(clip: dict, game_name: str, game: TrackedGame | None) -> VideoCandidate | None:
    cid, url = clip.get("id"), clip.get("url")
    if not cid or not url:
        return None
    views = int(clip.get("view_count", 0))
    duration = float(clip.get("duration", 0) or 0)
    score = clip_trend_score(views, clip.get("created_at"))
    if game and game.primary:
        score += settings.PRIMARY_GAME_BOOST
    return VideoCandidate(
        platform=Platform.TWITCH,
        source_id=str(cid),
        url=url,
        title=clip.get("title", "") or "",
        creator=clip.get("broadcaster_name", "") or "",
        views=views,
        duration_s=int(round(duration)) or None,
        published_at=clip.get("created_at"),
        thumbnail=clip.get("thumbnail_url"),
        score=round(score, 2),
        extra={
            "is_clip": True,
            "language": clip.get("language"),
            "game_id": clip.get("game_id"),
            "game_name": game_name,
            "primary_game": bool(game and game.primary),
            "creator_name": clip.get("creator_name"),
            # Pour repérer un même moment clippé par plusieurs viewers.
            "video_id": clip.get("video_id") or "",
            "vod_offset": clip.get("vod_offset"),
        },
    )


def _drop_reason(clip: dict, langs: set[str], min_views: int) -> str | None:
    if langs and clip.get("language") not in langs:
        return "langue"
    if float(clip.get("duration", 0) or 0) < settings.SOURCE_CLIP_MIN_S:
        return "durée"
    if int(clip.get("view_count", 0)) < min_views:
        return "vues"
    return None


def select_clips(clips: list[dict], langs: set[str]) -> tuple[list[dict], int, dict[str, int]]:
    """Filtre les clips d'un jeu avec un seuil de vues ADAPTATIF.

    On part de TWITCH_CLIP_MIN_VIEWS ; si une langue n'a pas assez de clips
    (jeu pas encore sorti, heure creuse, petit marché FR), on divise le seuil
    par 3 jusqu'au plancher — mieux vaut laisser le LLM juger un clip à 40 vues
    que ne rien publier. Retourne (gardés, seuil appliqué, raisons de rejet).
    """
    need = settings.TWITCH_MIN_POOL_PER_LANG
    min_views = settings.TWITCH_CLIP_MIN_VIEWS
    floor = min(settings.TWITCH_CLIP_FLOOR_VIEWS, min_views)
    while True:
        kept, reasons = [], {}
        for c in clips:
            r = _drop_reason(c, langs, min_views)
            if r:
                reasons[r] = reasons.get(r, 0) + 1
            else:
                kept.append(c)
        per_lang = {l: sum(1 for c in kept if c.get("language") == l) for l in (langs or {"*"})}
        enough = all(n >= need for n in per_lang.values()) if langs else len(kept) >= need
        if enough or min_views <= floor:
            return kept, min_views, reasons
        min_views = max(floor, min_views // 3)


def detect() -> list[VideoCandidate]:
    """Clips viraux des jeux suivis (+ créateurs ciblés en mode campagne)."""
    langs = set(settings.TWITCH_LANGUAGES)
    started_at = (
        datetime.now(timezone.utc) - timedelta(days=settings.TWITCH_CLIPS_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates: dict[str, VideoCandidate] = {}

    # --- 1) Clips par jeu ---------------------------------------------------
    if not (settings.TARGET_ONLY and settings.TARGET_STREAMERS_TWITCH):
        games = _resolve_games(tracked_games())
        for game_id, g in games.items():
            try:
                clips = _paged_clips(
                    {"game_id": game_id, "started_at": started_at}, settings.TWITCH_CLIPS_PAGES
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Twitch: clips de '%s' indisponibles : %s", g.info.name, exc)
                continue
            selected, min_views, reasons = select_clips(clips, langs)
            kept = 0
            for clip in selected:
                cand = _clip_to_candidate(clip, g.info.name, g)
                if cand and cand.uid not in candidates:
                    candidates[cand.uid] = cand
                    kept += 1
            lang_hist: dict[str, int] = {}
            for clip in clips:
                lang_hist[clip.get("language") or "?"] = lang_hist.get(clip.get("language") or "?", 0) + 1
            top_langs = sorted(lang_hist.items(), key=lambda kv: -kv[1])[:6]
            views = sorted((int(c.get("view_count", 0)) for c in clips), reverse=True)
            log.info(
                "Twitch: %s%s — %d clips lus, %d retenus (seuil %d vues) | rejets %s | langues %s | "
                "vues max/médiane/min %s/%s/%s",
                g.info.name, " [prioritaire]" if g.primary else "", len(clips), kept, min_views,
                reasons or "aucun", top_langs,
                views[0] if views else 0, views[len(views) // 2] if views else 0,
                views[-1] if views else 0,
            )

    # --- 2) Créateurs ciblés (campagnes payées à la vue) ---------------------
    targets = [t.lower() for t in settings.TARGET_STREAMERS_TWITCH]
    if targets:
        try:
            users = _helix_get("users", {"login": targets}).get("data", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("Twitch: créateurs ciblés introuvables : %s", exc)
            users = []
        raw: list[dict] = []
        for u in users:
            try:
                raw.extend(_paged_clips({"broadcaster_id": u["id"], "started_at": started_at}, 1))
            except Exception as exc:  # noqa: BLE001
                log.warning("Twitch: clips de %s indisponibles : %s", u.get("login"), exc)
        names = _games_names([c.get("game_id") for c in raw])
        for clip in raw:
            if int(clip.get("view_count", 0)) < settings.TWITCH_CLIP_MIN_VIEWS:
                continue
            gname = names.get(str(clip.get("game_id")), "")
            g = match_game(gname)
            if not g and not settings.TARGET_ANY_GAME:
                continue  # hors des jeux suivis
            cand = _clip_to_candidate(clip, gname, g)
            if cand:
                cand.score = round(cand.score + settings.TARGET_SCORE_BOOST, 2)
                cand.extra["target"] = True
                candidates[cand.uid] = cand
        log.info("Twitch: %d créateur(s) ciblé(s) scanné(s)", len(users))

    result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    log.info("Twitch: %d clips candidats", len(result))
    return result


if __name__ == "__main__":  # python -m src.detection.twitch
    settings.validate(["twitch"])
    for c in detect()[:15]:
        print(f"[{c.score:6.2f}] {c.views:>7} vues | {c.extra.get('game_name')} | {c.creator} — {c.title[:60]}")
        print(f"          {c.url}  ({c.duration_s}s, {c.extra.get('language')})")
