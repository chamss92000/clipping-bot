"""Rapport de run — déposé sur Drive à chaque cycle (`rapport.md`, écrasé).

Contient : les clips produits (titre retenu + alternatives + légende prête à
copier), les clips écartés par le LLM (note + pourquoi), le classement des
candidats et les clips écartés à la détection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import settings
from src.models import VideoCandidate


def _signal(c: VideoCandidate) -> str:
    return f"{c.views:,} vues · {c.extra.get('game_name') or '?'} · {c.extra.get('language') or '?'}".replace(",", " ")


def _local_now() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%d/%m/%Y %H:%M")
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def build_report(
    candidates: list[VideoCandidate],
    rejected: list[tuple[VideoCandidate, str]],
    chosen: list[VideoCandidate],
    clips: list[dict[str, Any]],
    llm_rejected: list[tuple[VideoCandidate, dict]] | None = None,
    top_n: int = 15,
) -> str:
    lines: list[str] = [f"# Rapport clipping-bot — {_local_now()}", ""]
    folder = {"fr": settings.DRIVE_ROOT_FOLDER_NAME_FR, "intl": settings.DRIVE_ROOT_FOLDER_NAME}

    lines += [f"## Clips produits ({len(clips)})", "",
              "_Légende prête à copier/coller. Les titres alternatifs servent si tu veux varier._", ""]
    if clips:
        for mk in ("fr", "intl"):
            mk_clips = [c for c in clips if (c.get("market") or "intl") == mk]
            if not mk_clips:
                continue
            label = "🇫🇷 Français" if mk == "fr" else "🌍 International"
            lines += [f"### {label} — dossier Drive `{folder.get(mk, mk)}/clips`", ""]
            for c in mk_clips:
                score = c.get("score")
                lines.append(f"- **{c.get('hook', '(sans titre)')}**"
                             + (f" — note {score:.0f}/100" if score is not None else ""))
                lines.append(f"  - fichier : `{c.get('clip')}` · {c.get('game') or ''} · {c.get('creator') or ''}")
                if c.get("alternatives"):
                    lines.append(f"  - autres titres : {' / '.join(c['alternatives'])}")
                lines.append("  - légende :")
                lines.append("")
                lines.append("    ```")
                lines += [f"    {ln}" for ln in (c.get("caption") or "").splitlines()]
                lines.append("    ```")
                if c.get("source_url"):
                    lines.append(f"  - source : {c['source_url']}")
            lines.append("")
    else:
        lines += ["_Aucun clip produit lors de ce cycle._", ""]

    if llm_rejected:
        lines += [f"## Clips écartés par le LLM ({len(llm_rejected)})", "",
                  f"_Note < {settings.CLIP_MIN_SCORE:.0f}/100 (réglable : variable CLIP_MIN_SCORE)._", ""]
        for c, brief in llm_rejected:
            lines.append(f"- {brief.get('score', 0):.0f}/100 · **{c.creator}** — {c.title[:60]}")
            if brief.get("what_happens"):
                lines.append(f"  - _{brief['what_happens']}_")
        lines.append("")

    lines += ["## Clips essayés ce cycle", ""]
    for c in chosen:
        lines.append(f"- **{c.creator}** — {c.title[:70]} · score {c.score} · {_signal(c)} · {c.url}")
    if not chosen:
        lines.append("_Aucun._")
    lines.append("")

    lines += [f"## Meilleurs candidats détectés (top {top_n})", "",
              "| # | Score | Plateforme | Jeu | Créateur | Vues | Titre |",
              "|---|-------|-----------|-----|----------|------|-------|"]
    for i, c in enumerate(candidates[:top_n], 1):
        title = (c.title or "").replace("|", "/")[:50]
        lines.append(f"| {i} | {c.score} | {c.platform.value} | {c.extra.get('game_name') or ''} "
                     f"| {c.creator} | {c.views} | {title} |")
    lines.append("")

    lines += [f"## Écartés à la détection ({len(rejected)})", ""]
    for c, reason in rejected[:25]:
        lines.append(f"- ~~{c.creator} — {(c.title or '')[:60]}~~ → _{reason}_")
    if not rejected:
        lines.append("_Aucun._")
    lines += ["", "---",
              "_Score de détection : vélocité (vues/heure) du clip + bonus jeu prioritaire "
              f"(+{settings.PRIMARY_GAME_BOOST:.0f}). Note LLM : potentiel viral jugé en regardant le clip._"]
    return "\n".join(lines) + "\n"
