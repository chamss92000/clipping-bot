"""Orchestrateur clips "cute/animaux/satisfying" (public thaï/asie) via Pexels.

Récupère des vidéos libres de droits (Pexels), recadre en 9:16 (fond flou =>
l'animal entier reste visible), dépose sur un dossier Drive dédié. Aucune
génération, aucun sous-titre (contenu universel, parfait pour la Thaïlande).
Réutilise PEXELS_API_KEY (déjà configurée).

    python cute_main.py
    DRY_RUN=true python cute_main.py
"""

from __future__ import annotations

import re
import unicodedata

from config import settings
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("cute_main")


def _slug(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-")


def run() -> int:
    log.info("=== Démarrage cycle Cute (DRY_RUN=%s) ===", settings.DRY_RUN)
    if not settings.CUTE_ENABLE:
        log.info("CUTE_ENABLE=false — rien à faire.")
        return 0
    attach_file_handler(settings.WORK_DIR / "cute.log")

    from src.editing.clip import make_vertical_clip
    from src.sourcing import pexels_clips

    try:
        clips = pexels_clips.top_videos()
    except settings.ConfigError as exc:
        log.error("Pexels non configuré (%s) — ajoute PEXELS_API_KEY.", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.error("Cute: détection échouée : %s", exc)
        return 1
    if not clips:
        log.warning("Cute: aucune vidéo détectée.")
        return 0

    try:
        from src.storage.drive import DriveStorage

        settings.validate(["drive"])
        storage = DriveStorage.from_settings()
        state = storage.load_state()
        root = storage.named_root(settings.CUTE_DRIVE_FOLDER_NAME)
    except Exception as exc:  # noqa: BLE001
        log.error("Storage Drive indisponible (%s) — cycle interrompu.", exc)
        return 1

    produced: list[dict] = []
    for clip in clips:
        if len(produced) >= settings.CUTE_PER_RUN:
            break
        if state.is_processed(clip.uid):
            continue
        try:
            raw = pexels_clips.download(clip, settings.DOWNLOAD_DIR)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cute: download %s KO : %s", clip.uid, exc)
            continue

        name = f"{_slug(clip.query)}_{clip.id}.mp4"
        out = settings.CLIPS_DIR / name
        try:
            make_vertical_clip(raw, 0.0, settings.CUTE_CLIP_MAX_S, out)
        except Exception as exc:  # noqa: BLE001
            log.error("Cute: montage %s KO : %s", clip.uid, exc)
            continue

        if not settings.DRY_RUN:
            try:
                storage.upload(out, name, subdir=settings.DRIVE_SUBDIR_CLIPS, root=root)
            except Exception as exc:  # noqa: BLE001
                log.warning("Cute: upload %s KO : %s", name, exc)
        produced.append({"clip": name, "title": clip.title, "query": clip.query, "url": clip.url})
        state.mark_processed(clip.uid)
        storage.save_state(state)
        log.info("Cute: ✅ %s (%s)", name, clip.query)

    if not produced:
        log.warning("Cute: 0 clip produit ce cycle.")
        return 0
    if settings.DRY_RUN:
        return 0

    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        try:
            storage.cleanup_old(settings.DRIVE_SUBDIR_CLIPS, settings.DRIVE_CLEANUP_MAX_AGE_H,
                                root=root, hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cute: nettoyage échoué : %s", exc)

    if settings.NOTIFY_EMAIL_ENABLE:
        try:
            from src.notify import _folder_url, send_email

            url = _folder_url(root)
            items = "".join(
                f"<li><b>{c['query']}</b> — <code>{c['clip']}</code></li>" for c in produced
            )
            html = (
                "<div style='font-family:system-ui,Arial,sans-serif;font-size:15px'>"
                f"<h2>{len(produced)} clip(s) cute prêt(s)</h2>"
                f"<p><a href='{url}'>Ouvrir le dossier Drive</a></p><ul>{items}</ul>"
                "<p style='color:#888;font-size:13px'>Poste sur ta chaîne ciblée Thaïlande + "
                "ajoute un son tendance local.</p></div>"
            )
            send_email(f"🐾 clipping-bot : {len(produced)} clip(s) cute prêt(s)", html,
                       f"{len(produced)} clips cute prêts\n{url}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Cute: notification e-mail échouée : %s", exc)

    log.info("=== Cycle Cute terminé : %d clip(s) ===", len(produced))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
