"""Runner de test du Bloc 1.

Usage :
    python -m src.detection                 # toutes les sources
    python -m src.detection youtube twitch  # sous-ensemble
    python -m src.detection --json          # sortie JSON brute

Valide seulement les secrets des sources demandées, exécute la détection et
affiche un classement lisible (ou du JSON avec --json).
"""

from __future__ import annotations

import json
import sys

from config import settings
from src.models import Platform

from . import detect_all

_ALIASES = {p.value: p for p in Platform}


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv

    enabled = [_ALIASES[a] for a in args if a in _ALIASES] or list(Platform)

    # On ne valide que les blocs correspondant aux sources activées.
    blocks = [p.value for p in enabled]
    try:
        settings.validate(blocks)
    except settings.ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        # On continue quand même : detect_all skippe proprement les sources
        # dont la config manque (utile pour tester Kick seul, par ex.).

    candidates = detect_all(enabled=enabled)

    if as_json:
        print(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2))
        return 0

    if not candidates:
        print("Aucun candidat détecté.")
        return 0

    print(f"\n=== {len(candidates)} candidats détectés ===\n")
    for i, c in enumerate(candidates, 1):
        extra = ""
        if c.platform in (Platform.TWITCH, Platform.KICK):
            extra = f" live={c.extra.get('live_viewer_count')}"
        print(
            f"{i:>2}. [{c.platform.value:<7}] score={c.score:6.2f} "
            f"vues={c.views:>10,}{extra}"
        )
        print(f"    {c.creator} — {c.title[:72]}")
        print(f"    {c.url}  ({c.duration_s}s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
