"""Modèles de données partagés entre tous les blocs du pipeline.

On utilise des dataclasses simples (sérialisables en JSON via `to_dict`) plutôt
que pydantic pour rester léger et sans dépendance supplémentaire.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Platform(str, Enum):
    """Plateforme source d'une vidéo candidate."""

    YOUTUBE = "youtube"
    TWITCH = "twitch"
    KICK = "kick"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class VideoCandidate:
    """Une vidéo/VOD détectée, candidate au traitement.

    C'est le contrat de sortie du Bloc 1 (détection) et l'entrée du Bloc 2
    (téléchargement). `uid` sert de clé stable de déduplication et de tracking
    dans le `state.json`.
    """

    platform: Platform
    source_id: str  # id natif de la plateforme (videoId YouTube, id VOD Twitch/Kick)
    url: str
    title: str
    creator: str
    views: int = 0
    duration_s: int | None = None
    published_at: str | None = None  # ISO8601
    thumbnail: str | None = None
    #: Score de tri interne (rempli par la détection ; heuristique par plateforme).
    score: float = 0.0
    #: Métadonnées libres spécifiques à la plateforme (game, category, live...).
    extra: dict[str, Any] = field(default_factory=dict)
    detected_at: str = field(default_factory=_utcnow_iso)

    @property
    def uid(self) -> str:
        """Identifiant unique stable, cross-plateforme."""
        return f"{self.platform.value}:{self.source_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["platform"] = self.platform.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VideoCandidate":
        payload = dict(data)
        payload["platform"] = Platform(payload["platform"])
        return cls(**payload)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
