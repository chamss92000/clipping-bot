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

#: Pondération grossière pour équilibrer les échelles de score entre sources.
_PLATFORM_WEIGHT: dict[Platform, float] = {
    Platform.YOUTUBE: 1.0,
    Platform.TWITCH: 1.0,
    Platform.KICK: 0.9,
}

_SOURCES = {
    Platform.YOUTUBE: youtube.detect,
    Platform.TWITCH: twitch.detect,
    Platform.KICK: kick.detect,
}


def detect_all(
    enabled: list[Platform] | None = None,
    max_candidates: int | None = None,
) -> list[VideoCandidate]:
    """Agrège les candidats des sources activées, triés et plafonnés.

    :param enabled: sous-ensemble de plateformes à interroger (défaut : toutes).
    :param max_candidates: plafond de la file retournée (défaut settings).
    """
    enabled = enabled or list(_SOURCES.keys())
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

    # Déduplication cross-source par uid (sécurité ; peu probable de collision).
    unique: dict[str, VideoCandidate] = {}
    for c in all_candidates:
        if c.uid not in unique or c.score > unique[c.uid].score:
            unique[c.uid] = c

    ranked = sorted(unique.values(), key=lambda c: c.score, reverse=True)[:max_candidates]
    log.info(
        "Détection terminée : %d candidats retenus (sur %d bruts)",
        len(ranked),
        len(all_candidates),
    )
    return ranked


__all__ = ["detect_all", "youtube", "twitch", "kick"]
