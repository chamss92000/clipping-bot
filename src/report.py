"""Rapport de run — déposé sur Drive à chaque cycle.

Objectif : pouvoir **vérifier les sources choisies** sans lire les logs GitHub.
Le fichier `rapport.md` est écrasé à chaque run (dernier état) et contient :
  * le classement des sources détectées avec leur signal de tendance,
  * les sources écartées par le filtre qualité (et pourquoi),
  * la source retenue et les clips produits (accroche, score viral, YouTube).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config import settings
from src.models import Platform, VideoCandidate


def _signal(c: VideoCandidate) -> str:
    """Signal de tendance lisible selon la plateforme."""
    if c.platform == Platform.YOUTUBE:
        vph = c.extra.get("views_per_hour")
        age = c.extra.get("age_hours")
        if vph is not None:
            return f"{vph:,} vues/h · {c.views:,} vues · {age}h".replace(",", " ")
        return f"{c.views:,} vues".replace(",", " ")
    live = c.extra.get("live_viewer_count")
    if live is not None:
        return f"{live:,} viewers live · {c.views:,} vues VOD".replace(",", " ")
    return f"{c.views:,} vues".replace(",", " ")


def _local_now() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%d/%m/%Y %H:%M")
    except Exception:  # noqa: BLE001
        return datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")


def build_report(
    candidates: list[VideoCandidate],
    rejected: list[tuple[VideoCandidate, str]],
    chosen: list[VideoCandidate],
    clips: list[dict[str, Any]],
    youtube_left: int,
    top_n: int = 10,
) -> str:
    lines: list[str] = []
    lines.append(f"# Rapport clipping-bot — {_local_now()}")
    lines.append("")

    # --- Clips produits (le plus important en premier) ---
    # Groupés par marché : chaque marché = un dossier Drive = une chaîne.
    _folder = {
        "fr": settings.DRIVE_ROOT_FOLDER_NAME_FR,
        "intl": settings.DRIVE_ROOT_FOLDER_NAME,
    }
    lines.append(f"## Clips produits ({len(clips)})")
    lines.append("")
    lines.append("_Copie/colle le hook + les hashtags ci-dessous en légende quand tu postes._")
    lines.append("")
    if clips:
        for mk in ("fr", "intl"):
            mk_clips = [c for c in clips if (c.get("market") or "intl") == mk]
            if not mk_clips:
                continue
            label = "🇫🇷 Français" if mk == "fr" else "🌍 International"
            lines.append(f"### {label} — dossier Drive `{_folder.get(mk, mk)}/clips`")
            lines.append("")
            for c in mk_clips:
                tags = " ".join(c.get("hashtags") or [])
                lines.append(f"- **{c.get('hook','(sans titre)')}**")
                lines.append(f"  - fichier : `{c.get('clip')}`")
                lines.append(f"  - légende : {c.get('hook','')} {tags}".rstrip())
                yt = c.get("youtube")
                if yt:
                    yt_txt = {
                        "posted": "✅ publié sur YouTube Shorts",
                        "failed": "❌ échec YouTube",
                        "dry_run": "🧪 simulé (dry-run)",
                    }.get(yt, str(yt))
                    lines.append(f"  - YouTube : {yt_txt}")
            lines.append("")
    else:
        lines.append("_Aucun clip produit lors de ce cycle._")
        lines.append("")

    # --- Source(s) retenue(s) ---
    lines.append("## Source(s) traitée(s)")
    lines.append("")
    if chosen:
        for c in chosen:
            lines.append(f"- **{c.creator}** — {c.title}")
            lines.append(f"  - {c.platform.value} · score tendance **{c.score}/100** · {_signal(c)}")
            lines.append(f"  - {c.url}")
    else:
        lines.append("_Aucune source traitée._")
    lines.append("")

    # --- Classement des candidats ---
    lines.append(f"## Sources les plus tendances détectées (top {top_n})")
    lines.append("")
    lines.append("| # | Score | Plateforme | Créateur | Signal | Titre |")
    lines.append("|---|-------|-----------|----------|--------|-------|")
    for i, c in enumerate(candidates[:top_n], 1):
        title = (c.title or "").replace("|", "/")[:60]
        lines.append(
            f"| {i} | {c.score} | {c.platform.value} | {c.creator} | {_signal(c)} | {title} |"
        )
    lines.append("")

    # --- Sources écartées ---
    lines.append(f"## Sources écartées par le filtre qualité ({len(rejected)})")
    lines.append("")
    if rejected:
        for c, reason in rejected[:20]:
            lines.append(f"- ~~{c.creator} — {(c.title or '')[:60]}~~ → _{reason}_")
    else:
        lines.append("_Aucune._")
    lines.append("")

    lines.append("---")
    lines.append(
        "_Score de tendance 0-100 : vélocité (vues/heure) pour YouTube, "
        "audience live pour Twitch/Kick — échelle commune aux 3 plateformes._"
    )
    return "\n".join(lines) + "\n"
