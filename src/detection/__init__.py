"""Bloc 1 — Détection des tendances (YouTube + Twitch + Kick).

Point d'entrée : `detect_all()` qui interroge les trois sources en isolation
(l'échec d'une source ne casse jamais les autres), fusionne, dédoublonne et
trie les candidats par score décroissant.

Le score n'est pas comparable *tel quel* d'une plateforme à l'autre (échelles
de vues différentes) ; on applique donc un léger poids par plateforme pour
équilibrer la file finale, ajustable via la constante `_PLATFORM_WEIGHT`.
"""

from __future__ import annotations

from config import settings
from src.models import Platform, VideoCandidate
from src.utils.logging import get_logger

from . import kick, twitch, youtube

log = get_logger("detection")

#: Les scores sont maintenant sur une échelle 0-100 commune (vélocité YouTube /
#: audience live Twitch-Kick), donc plus besoin de rééquilibrer. On garde juste
#: une légère décote Kick (données moins fiables).
_PLATFORM_WEIGHT: dict[Platform, float] = {
    Platform.YOUTUBE: 1.0,
    Platform.TWITCH: 1.0,
    Platform.KICK: 0.95,
}

#: Sources écartées au dernier `detect_all` — [(candidat, raison)], pour le rapport.
LAST_REJECTED: list[tuple[VideoCandidate, str]] = []

_SOURCES = {
    Platform.YOUTUBE: youtube.detect,
    Platform.TWITCH: twitch.detect,
    Platform.KICK: kick.detect,
}


def market_of(c: VideoCandidate) -> str:
    """Classe une source dans un marché : "fr" (francophone) ou "intl".

    Sert à router le clip vers le bon dossier Drive (=> la bonne chaîne).
    Basé sur la langue quand elle est connue (Twitch clips, YouTube), sinon sur
    un défaut par plateforme (Kick : liste de chaînes FR connues).
    """
    lang = (c.extra.get("language") or "").lower()
    if lang:
        return "fr" if lang.startswith("fr") else "intl"
    if c.platform == Platform.YOUTUBE:
        return "fr" if settings.YOUTUBE_REGION.upper() == "FR" else "intl"
    if c.platform == Platform.KICK:
        who = (c.extra.get("slug") or c.creator or "").lower()
        fr_set = {s.lower() for s in settings.KICK_FR_CHANNELS}
        return "fr" if who in fr_set else "intl"
    return "intl"


def _is_low_quality(c: VideoCandidate) -> str | None:
    """Retourne la raison du rejet si la source est du contenu 'recyclé', sinon None.

    C'est le filtre le plus déterminant sur la qualité finale : un récap de film
    ou une compilation ne produira jamais un bon clip (pas de visage, pas de
    réaction), quel que soit le montage.
    """
    title = (c.title or "").lower()
    creator = (c.creator or "").lower()
    for kw in settings.TITLE_BLACKLIST:
        if kw and kw.lower() in title:
            return f"titre contient '{kw}'"
    for kw in settings.CHANNEL_BLACKLIST:
        if kw and kw.lower() in creator:
            return f"chaîne blacklistée '{kw}'"
    return None


def detect_all(
    enabled: list[Platform] | None = None,
    max_candidates: int | None = None,
) -> list[VideoCandidate]:
    """Agrège les candidats des sources activées, triés et plafonnés.

    :param enabled: sous-ensemble de plateformes à interroger (défaut : toutes).
    :param max_candidates: plafond de la file retournée (défaut settings).
    """
    if enabled is None:
        enabled = list(_SOURCES.keys())
        # YouTube n'est pas téléchargeable en CI (anti-bot) : on ne le détecte
        # que s'il est explicitement activé, pour ne pas gaspiller d'essais.
        if not settings.YOUTUBE_SOURCE_ENABLE and Platform.YOUTUBE in enabled:
            enabled.remove(Platform.YOUTUBE)
            log.info("YouTube désactivé comme source (YOUTUBE_SOURCE_ENABLE=false).")
    max_candidates = max_candidates or settings.MAX_CANDIDATES

    all_candidates: list[VideoCandidate] = []
    for platform in enabled:
        detector = _SOURCES[platform]
        try:
            found = detector()
        except settings.ConfigError as exc:
            log.warning("Détection %s ignorée (config manquante) : %s", platform.value, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - isole la panne d'une source
            log.error("Détection %s a échoué : %s", platform.value, exc, exc_info=True)
            continue
        for c in found:
            c.score = round(c.score * _PLATFORM_WEIGHT.get(platform, 1.0), 4)
        all_candidates.extend(found)
        log.info("Source %s : %d candidats", platform.value, len(found))

    # Filtre qualité : on écarte le contenu recyclé/narration avant tout traitement.
    LAST_REJECTED.clear()
    kept: list[VideoCandidate] = []
    for c in all_candidates:
        reason = _is_low_quality(c)
        if reason:
            LAST_REJECTED.append((c, reason))
            log.info("Écarté (%s) : %s — %s", reason, c.creator, c.title[:60])
        else:
            kept.append(c)
    if LAST_REJECTED:
        log.info("Filtre qualité : %d source(s) écartée(s)", len(LAST_REJECTED))

    # Déduplication cross-source par uid (sécurité ; peu probable de collision).
    unique: dict[str, VideoCandidate] = {}
    for c in kept:
        if c.uid not in unique or c.score > unique[c.uid].score:
            unique[c.uid] = c

    ranked = sorted(unique.values(), key=lambda c: c.score, reverse=True)[:max_candidates]
    log.info(
        "Détection terminée : %d candidats retenus (sur %d bruts)",
        len(ranked),
        len(all_candidates),
    )
    return ranked


__all__ = ["detect_all", "market_of", "youtube", "twitch", "kick"]
