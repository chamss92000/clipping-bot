"""Orchestrateur clips "Asie" (public thaï, + Corée si dispo).

Source = clips Twitch DÉJÀ viraux de la communauté thaï (API Helix, filtrés par
langue, téléchargeables en CI). Simple à récupérer : aucune génération, aucun
sous-titre (donc aucun souci de police thaï). On recadre juste en 9:16 et on
dépose sur un dossier Drive dédié.

    python asia_main.py
    DRY_RUN=true python asia_main.py   # produit en local sans uploader
"""

from __future__ import annotations

import re
import unicodedata

from config import settings
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("asia_main")


def _slug(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-")


def run() -> int:
    log.info("=== Démarrage cycle Asie (DRY_RUN=%s) ===", settings.DRY_RUN)
    if not settings.ASIA_ENABLE:
        log.info("ASIA_ENABLE=false — rien à faire.")
        return 0
    settings.validate(["twitch"])
    attach_file_handler(settings.WORK_DIR / "asia.log")

    from src.detection import twitch
    from src.downloader.download import BotCheckError, download, is_bot_check
    from src.editing.clip import make_vertical_clip

    # Seuil de vues par clip adapté aux streamers thaï (plus petits).
    settings.TWITCH_CLIP_MIN_VIEWS = settings.ASIA_CLIP_MIN_VIEWS
    clips = twitch.detect_clips(
        min_viewers=settings.ASIA_MIN_VIEWERS, languages=settings.ASIA_LANGUAGES
    )
    if not clips:
        log.warning("Asie: aucun clip détecté.")
        return 0

    # Storage / dédup (state partagé) + dossier dédié.
    try:
        from src.storage.drive import DriveStorage

        settings.validate(["drive"])
        storage = DriveStorage.from_settings()
        state = storage.load_state()
        root = storage.named_root(settings.ASIA_DRIVE_FOLDER_NAME)
    except Exception as exc:  # noqa: BLE001
        log.error("Storage Drive indisponible (%s) — cycle interrompu.", exc)
        return 1

    produced: list[dict] = []
    for cand in clips:
        if len(produced) >= settings.ASIA_PER_RUN:
            break
        if state.is_processed(cand.uid):
            continue
        try:
            dl = download(cand, settings.DOWNLOAD_DIR)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, BotCheckError) or is_bot_check(exc):
                log.warning("Asie: %s bloqué (anti-bot) — skip.", cand.uid)
            else:
                log.warning("Asie: téléchargement %s échoué : %s", cand.uid, exc)
            continue

        dur = min(dl.duration_s or cand.duration_s or 30, settings.CLIP_MAX_DURATION_S)
        name = f"{_slug(cand.title) or 'clip'}_{cand.source_id[:6]}.mp4"
        out = settings.CLIPS_DIR / name
        try:
            make_vertical_clip(dl.path, 0.0, dur, out)
        except Exception as exc:  # noqa: BLE001
            log.error("Asie: montage %s échoué : %s", cand.uid, exc)
            continue

        if not settings.DRY_RUN:
            try:
                storage.upload(out, name, subdir=settings.DRIVE_SUBDIR_CLIPS, root=root)
            except Exception as exc:  # noqa: BLE001
                log.warning("Asie: upload %s échoué : %s", name, exc)
        produced.append({"clip": name, "title": cand.title, "creator": cand.creator, "url": cand.url})
        state.mark_processed(cand.uid)
        storage.save_state(state)
        log.info("Asie: ✅ %s (%s)", name, cand.creator)

    if not produced:
        log.warning("Asie: 0 clip produit ce cycle.")
        return 0
    if settings.DRY_RUN:
        return 0

    # Nettoyage + notif
    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        try:
            storage.cleanup_old(settings.DRIVE_SUBDIR_CLIPS, settings.DRIVE_CLEANUP_MAX_AGE_H,
                                root=root, hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE)
        except Exception as exc:  # noqa: BLE001
            log.warning("Asie: nettoyage échoué : %s", exc)

    if settings.NOTIFY_EMAIL_ENABLE:
        try:
            from src.notify import _folder_url, send_email

            url = _folder_url(root)
            items = "".join(
                f"<li><b>{c['creator']}</b> — {c['title'][:80]}<br>"
                f"<span style='color:#555'>fichier : <code>{c['clip']}</code></span></li>"
                for c in produced
            )
            html = (
                "<div style='font-family:system-ui,Arial,sans-serif;font-size:15px'>"
                f"<h2>{len(produced)} clip(s) thaï prêt(s)</h2>"
                f"<p><a href='{url}'>Ouvrir le dossier Drive</a></p><ul>{items}</ul>"
                "<p style='color:#888;font-size:13px'>Poste sur ta chaîne ciblée Thaïlande + "
                "ajoute un son tendance local.</p></div>"
            )
            send_email(f"🇹🇭 clipping-bot : {len(produced)} clip(s) thaï prêt(s)", html,
                       f"{len(produced)} clips thaï prêts\n{url}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Asie: notification e-mail échouée : %s", exc)

    log.info("=== Cycle Asie terminé : %d clip(s) ===", len(produced))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
