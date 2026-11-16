"""Catalogue des jeux suivis : correspondance de noms, priorité, hashtags, contexte LLM.

Point unique de vérité pour « ce clip parle-t-il d'un jeu qu'on suit ? ». Les
noms de catégorie varient selon la plateforme ("Grand Theft Auto VI" sur
Twitch, parfois "GTA 6"/"GTA VI" ailleurs) : on normalise via des alias.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import settings


@dataclass(frozen=True)
class GameInfo:
    #: Nom canonique (= nom exact de la catégorie Twitch).
    name: str
    aliases: tuple[str, ...] = ()
    #: Hashtags de niche (les plus recherchés en premier).
    hashtags: tuple[str, ...] = ()
    #: Contexte injecté au LLM (vocabulaire, persos, lieux) pour des titres précis.
    context: str = ""


#: Jeux connus. Un jeu configuré mais absent d'ici marche quand même (hashtag
#: dérivé du nom, pas de contexte) : ce catalogue ne fait qu'ENRICHIR.
_KNOWN: dict[str, GameInfo] = {
    "grand theft auto vi": GameInfo(
        name="Grand Theft Auto VI",
        aliases=("gta 6", "gta vi", "gta6", "gtavi", "grand theft auto 6"),
        hashtags=("#GTA6", "#GTAVI", "#gta6gameplay", "#gaming"),
        context=(
            "GTA 6 (Rockstar Games) : État de Leonida (inspiré de la Floride), "
            "ville de Vice City, duo de protagonistes Lucia et Jason. Énorme hype : "
            "les viewers veulent voir le gameplay, les découvertes, les bugs, les "
            "courses-poursuites, les moments WTF et les réactions des streamers."
        ),
    ),
    "grand theft auto v": GameInfo(
        name="Grand Theft Auto V",
        aliases=("gta 5", "gta v", "gta5", "gtav", "gta rp", "gta online"),
        hashtags=("#GTA5", "#GTARP", "#gaming"),
        context=(
            "GTA V / GTA Online / GTA RP (serveurs roleplay). Moments typiques : "
            "braquages, courses-poursuites avec la police, scènes RP absurdes, "
            "trolls, réactions."
        ),
    ),
}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (name or "").lower())).strip()


@dataclass
class TrackedGame:
    info: GameInfo
    primary: bool
    keys: set[str] = field(default_factory=set)


def _build(name: str, primary: bool) -> TrackedGame:
    info = _KNOWN.get(_norm(name)) or GameInfo(name=name.strip())
    keys = {_norm(info.name), *(_norm(a) for a in info.aliases)}
    return TrackedGame(info=info, primary=primary, keys=keys)


def tracked_games() -> list[TrackedGame]:
    """Jeux suivis, prioritaires d'abord (ordre de la config)."""
    out: list[TrackedGame] = []
    seen: set[str] = set()
    for names, primary in ((settings.GAMES_PRIMARY, True), (settings.GAMES_SECONDARY, False)):
        for n in names:
            if n and _norm(n) not in seen:
                seen.add(_norm(n))
                out.append(_build(n, primary))
    return out


def match_game(category: str | None) -> TrackedGame | None:
    """Retourne le jeu suivi correspondant à un nom de catégorie, ou None.

    Correspondance EXACTE sur le nom normalisé ou un alias : "Grand Theft Auto V"
    ne doit pas capturer "Grand Theft Auto VI" (et inversement).
    """
    key = _norm(category or "")
    if not key:
        return None
    for g in tracked_games():
        if key in g.keys:
            return g
    return None


def info_for(category: str | None) -> GameInfo:
    """Infos du jeu (catalogue) ; repli générique si le jeu est inconnu."""
    g = match_game(category)
    if g:
        return g.info
    return _KNOWN.get(_norm(category or "")) or GameInfo(name=(category or "").strip())


def game_hashtag(name: str) -> str:
    """Hashtag principal d'un jeu (#GTA6 pour GTA VI, sinon dérivé du nom)."""
    info = info_for(name)
    if info.hashtags:
        return info.hashtags[0]
    slug = re.sub(r"\W", "", info.name)
    return f"#{slug}" if slug else "#gaming"
