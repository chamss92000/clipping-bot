"""Bloc 1 — Détection des clips gaming viraux (Twitch + Kick), focus GTA 6.

Point d'entrée : `detect_all()` qui interroge les sources en isolation (l'échec
d'une source ne casse jamais l'autre), filtre, dédoublonne et trie.

Étapes de qualité (dans l'ordre) :
  1. filtre titre/chaîne (retransmissions esport, contenu recyclé) ;
  2. anti-doublon de MOMENT : le même passage clippé par plusieurs viewers
     (même streamer, même VOD, offsets proches) => on garde le plus vu ;
  3. diversité : au plus MAX_CLIPS_PER_STREAMER clips par streamer.
"""

from __future__ import annotations

from datetime import datetime

from config import settings
from src.models import Platform, VideoCandidate
from src.utils.logging import get_logger

from . import kick, twitch

log = get_logger("detection")

#: Sources écartées au dernier `detect_all` — [(candidat, raison)], pour le rapport.
LAST_REJECTED: list[tuple[VideoCandidate, str]] = []

_SOURCES = {
    Platform.TWITCH: twitch.detect,
    Platform.KICK: kick.detect,
}


def market_of(c: VideoCandidate) -> str:
    """Classe un clip dans un marché : "fr" (francophone) ou "intl".

    Sert à router le clip vers le bon dossier Drive (=> la bonne chaîne).
    """
    lang = (c.extra.get("language") or "").lower()
    if lang:
        return "fr" if lang.startswith("fr") else "intl"
    if c.platform == Platform.KICK:
        who = (c.extra.get("slug") or c.creator or "").lower()
        fr_set = {s.lower() for s in settings.KICK_FR_CHANNELS}
        return "fr" if who in fr_set else "intl"
    return "intl"


def _is_low_quality(c: VideoCandidate) -> str | None:
    """Raison du rejet si le clip est hors-cible, sinon None."""
    title = (c.title or "").lower()
    creator = (c.creator or "").lower()
    for kw in settings.TITLE_BLACKLIST:
        if kw and kw.lower() in title:
            return f"titre contient '{kw}'"
    for kw in settings.CHANNEL_BLACKLIST:
        if kw and kw.lower() in creator:
            return f"chaîne blacklistée '{kw}'"
    return None


def _moment_key(c: VideoCandidate) -> tuple[str, float] | None:
    """(référence, seconde) d'un moment : position dans la VOD si connue,
    sinon heure de création du clip (2 clips du même moment sont créés à
    quelques secondes/minutes d'écart)."""
    vid, off = c.extra.get("video_id"), c.extra.get("vod_offset")
    if vid and off is not None:
        return (f"vod:{vid}", float(off))
    if c.published_at:
        try:
            ts = datetime.fromisoformat(c.published_at.replace("Z", "+00:00")).timestamp()
            return ("t", ts)
        except ValueError:
            return None
    return None


def dedupe_moments(cands: list[VideoCandidate], window_s: float) -> tuple[list[VideoCandidate], list[VideoCandidate]]:
    """Garde 1 clip par moment (le mieux classé). Entrée triée par score décroissant.

    Retourne (gardés, doublons).
    """
    kept: list[VideoCandidate] = []
    dupes: list[VideoCandidate] = []
    seen: dict[str, list[tuple[str, float]]] = {}
    for c in cands:
        who = (c.creator or "").lower()
        key = _moment_key(c)
        if key is not None:
            prior = seen.setdefault(who, [])
            if any(ref == key[0] and abs(sec - key[1]) <= window_s for ref, sec in prior):
                dupes.append(c)
                continue
            prior.append(key)
        kept.append(c)
    return kept, dupes


def cap_per_creator(cands: list[VideoCandidate], cap: int) -> tuple[list[VideoCandidate], list[VideoCandidate]]:
    if cap <= 0:
        return cands, []
    count: dict[str, int] = {}
    kept, dropped = [], []
    for c in cands:
        who = (c.creator or "").lower()
        if count.get(who, 0) >= cap and not c.extra.get("target"):
            dropped.append(c)
            continue
        count[who] = count.get(who, 0) + 1
        kept.append(c)
    return kept, dropped


def detect_all(
    enabled: list[Platform] | None = None,
    max_candidates: int | None = None,
) -> list[VideoCandidate]:
    """Agrège les clips des sources activées, filtrés, dédoublonnés et triés."""
    enabled = enabled or list(_SOURCES.keys())
    max_candidates = max_candidates or settings.MAX_CANDIDATES

    all_candidates: list[VideoCandidate] = []
    for platform in enabled:
        try:
            found = _SOURCES[platform]()
        except settings.ConfigError as exc:
            log.warning("Détection %s ignorée (config manquante) : %s", platform.value, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - isole la panne d'une source
            log.error("Détection %s a échoué : %s", platform.value, exc, exc_info=True)
            continue
        all_candidates.extend(found)
        log.info("Source %s : %d candidats", platform.value, len(found))

    LAST_REJECTED.clear()
    kept: list[VideoCandidate] = []
    for c in all_candidates:
        reason = _is_low_quality(c)
        if reason:
            LAST_REJECTED.append((c, reason))
        else:
            kept.append(c)

    unique: dict[str, VideoCandidate] = {}
    for c in kept:
        if c.uid not in unique or c.score > unique[c.uid].score:
            unique[c.uid] = c
    ranked = sorted(unique.values(), key=lambda c: c.score, reverse=True)

    ranked, dupes = dedupe_moments(ranked, settings.DUPLICATE_WINDOW_S)
    LAST_REJECTED.extend((c, "même moment déjà clippé (doublon)") for c in dupes)
    ranked, capped = cap_per_creator(ranked, settings.MAX_CLIPS_PER_STREAMER)
    LAST_REJECTED.extend((c, "quota par streamer atteint") for c in capped)

    ranked = ranked[:max_candidates]
    log.info(
        "Détection terminée : %d candidats retenus (sur %d bruts ; %d écartés)",
        len(ranked), len(all_candidates), len(LAST_REJECTED),
    )
    return ranked


__all__ = ["detect_all", "market_of", "dedupe_moments", "cap_per_creator", "twitch", "kick"]
