"""Orchestrateur du contenu "cute / relax" (chaîne d'origine).

Indépendant du clipping : génère N vidéos relax (thumb-game) via
`src.generate.relax`, les dépose dans un dossier Drive dédié
(`clipping-bot-relax`), purge les anciennes (>48h) et envoie un e-mail récap.

Lancé par son propre cron GitHub Actions (workflow relax.yml), et en local :
    python relax_main.py
    DRY_RUN=true python relax_main.py   # génère en local sans uploader
"""

from __future__ import annotations

import time
from datetime import datetime

from config import settings
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("relax_main")


def run() -> int:
    log.info("=== Démarrage cycle relax (DRY_RUN=%s) ===", settings.DRY_RUN)
    if not settings.RELAX_ENABLE:
        log.info("RELAX_ENABLE=false — rien à faire.")
        return 0
    attach_file_handler(settings.WORK_DIR / "relax.log")

    from src.generate.relax import make_relax_video

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    produced: list[str] = []
    for i in range(settings.RELAX_PER_RUN):
        seed = int(time.time()) + i * 7919
        name = f"relax_{stamp}_{i + 1}.mp4"
        out = settings.CLIPS_DIR / name
        try:
            make_relax_video(out, seed=seed)
            produced.append(name)
        except Exception as exc:  # noqa: BLE001 - une vidéo ratée n'annule pas les autres
            log.error("Relax: génération de %s échouée : %s", name, exc, exc_info=True)

    if not produced:
        log.warning("Relax: aucune vidéo produite.")
        return 1

    if settings.DRY_RUN:
        log.info("DRY_RUN : %d vidéo(s) générée(s) en local, pas d'upload.", len(produced))
        return 0

    # --- Upload Drive (dossier dédié) + purge + notif ---
    try:
        from src.storage.drive import DriveStorage

        settings.validate(["drive"])
        storage = DriveStorage.from_settings()
        root = storage.named_root(settings.RELAX_DRIVE_FOLDER_NAME)
    except Exception as exc:  # noqa: BLE001
        log.error("Storage Drive indisponible (%s) — vidéos gardées en local.", exc)
        return 1

    for name in produced:
        try:
            storage.upload(settings.CLIPS_DIR / name, name,
                           subdir=settings.DRIVE_SUBDIR_CLIPS, root=root)
            log.info("Relax: ✅ déposé %s", name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Relax: upload de %s échoué : %s", name, exc)

    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        try:
            storage.cleanup_old(settings.DRIVE_SUBDIR_CLIPS, settings.DRIVE_CLEANUP_MAX_AGE_H,
                                root=root, hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE)
        except Exception as exc:  # noqa: BLE001
            log.warning("Relax: nettoyage échoué : %s", exc)

    if settings.NOTIFY_EMAIL_ENABLE:
        try:
            from src.notify import _folder_url, send_email

            url = _folder_url(root)
            items = "".join(f"<li><code>{n}</code></li>" for n in produced)
            html = (
                "<div style='font-family:system-ui,Arial,sans-serif;font-size:15px'>"
                f"<h2>{len(produced)} vidéo(s) relax prête(s)</h2>"
                f"<p><a href='{url}'>Ouvrir le dossier Drive</a></p><ul>{items}</ul>"
                "<p style='color:#888;font-size:13px'>Poste sur ta chaîne d'origine et "
                "ajoute un son tendance TikTok au moment de publier.</p></div>"
            )
            send_email(f"🧸 clipping-bot : {len(produced)} vidéo(s) relax prête(s)", html,
                       f"{len(produced)} vidéo(s) relax prêtes : {', '.join(produced)}\n{url}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Relax: notification e-mail échouée : %s", exc)

    log.info("=== Cycle relax terminé : %d vidéo(s) ===", len(produced))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
