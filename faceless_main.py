"""Orchestrateur du contenu faceless "valeur" (niche argent/IA).

Génère N vidéos faceless (voix off IA + sous-titres) via src.generate.faceless,
les dépose dans un dossier Drive dédié, purge les anciennes (>48h) et envoie un
e-mail récap (avec le hook + les hashtags à copier en légende).

    python faceless_main.py
    DRY_RUN=true python faceless_main.py   # génère en local sans uploader
"""

from __future__ import annotations

from datetime import datetime

from config import settings
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("faceless_main")


def run() -> int:
    log.info("=== Démarrage cycle faceless (DRY_RUN=%s) ===", settings.DRY_RUN)
    if not settings.FACELESS_ENABLE:
        log.info("FACELESS_ENABLE=false — rien à faire.")
        return 0
    settings.validate(["viral"])  # Gemini requis pour les scripts
    attach_file_handler(settings.WORK_DIR / "faceless.log")

    from src.generate.faceless import make_faceless_video

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    produced: list[dict] = []
    for i in range(settings.FACELESS_PER_RUN):
        name = f"ai_{stamp}_{i + 1}.mp4"
        try:
            meta = make_faceless_video(settings.CLIPS_DIR / name)
            meta["clip"] = name
            produced.append(meta)
        except Exception as exc:  # noqa: BLE001 - une vidéo ratée n'annule pas les autres
            log.error("Faceless: %s échouée : %s", name, exc, exc_info=True)

    if not produced:
        log.warning("Faceless: aucune vidéo produite.")
        return 1

    if settings.DRY_RUN:
        for m in produced:
            log.info("DRY_RUN: %s — %s", m["clip"], m["hook"])
        return 0

    try:
        from src.storage.drive import DriveStorage

        settings.validate(["drive"])
        storage = DriveStorage.from_settings()
        root = storage.named_root(settings.FACELESS_DRIVE_FOLDER_NAME)
    except Exception as exc:  # noqa: BLE001
        log.error("Storage Drive indisponible (%s) — vidéos gardées en local.", exc)
        return 1

    for m in produced:
        try:
            storage.upload(settings.CLIPS_DIR / m["clip"], m["clip"],
                           subdir=settings.DRIVE_SUBDIR_CLIPS, root=root)
            log.info("Faceless: ✅ déposé %s", m["clip"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Faceless: upload de %s échoué : %s", m["clip"], exc)

    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        try:
            storage.cleanup_old(settings.DRIVE_SUBDIR_CLIPS, settings.DRIVE_CLEANUP_MAX_AGE_H,
                                root=root, hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE)
        except Exception as exc:  # noqa: BLE001
            log.warning("Faceless: nettoyage échoué : %s", exc)

    if settings.NOTIFY_EMAIL_ENABLE:
        try:
            from src.notify import _folder_url, send_email

            url = _folder_url(root)
            items = "".join(
                f"<li><b>{m['hook']}</b><br><span style='color:#555'>fichier : "
                f"<code>{m['clip']}</code> · {m.get('duration','?')}s<br>hashtags : "
                f"{' '.join(m['hashtags'])}</span></li>"
                for m in produced
            )
            html = (
                "<div style='font-family:system-ui,Arial,sans-serif;font-size:15px'>"
                f"<h2>{len(produced)} vidéo(s) faceless prête(s)</h2>"
                f"<p><a href='{url}'>Ouvrir le dossier Drive</a></p><ul>{items}</ul>"
                "<p style='color:#888;font-size:13px'>Poste sur ta chaîne faceless et mets "
                "tes liens d'affiliation en bio.</p></div>"
            )
            send_email(f"🤖 clipping-bot : {len(produced)} vidéo(s) faceless prête(s)", html,
                       f"{len(produced)} vidéo(s) faceless prêtes\n{url}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Faceless: notification e-mail échouée : %s", exc)

    log.info("=== Cycle faceless terminé : %d vidéo(s) ===", len(produced))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
