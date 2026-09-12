"""Orchestrateur clips Reddit "cute/animaux/satisfying" (public thaï/asie).

Récupère les meilleures vidéos de subreddits populaires (API OAuth Reddit),
recadre en 9:16 (fond flou => l'animal entier reste visible, aucun crop de
travers), et dépose sur un dossier Drive dédié. Aucune génération, aucun
sous-titre (contenu universel, sans barrière de langue = parfait pour la Thaïlande).

    python reddit_main.py
    DRY_RUN=true python reddit_main.py
"""

from __future__ import annotations

import re
import unicodedata

from config import settings
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("reddit_main")


def _slug(text: str, max_len: int = 45) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-")


def run() -> int:
    log.info("=== Démarrage cycle Reddit (DRY_RUN=%s) ===", settings.DRY_RUN)
    if not settings.REDDIT_ENABLE:
        log.info("REDDIT_ENABLE=false — rien à faire.")
        return 0
    attach_file_handler(settings.WORK_DIR / "reddit.log")

    from src.editing.clip import make_vertical_clip
    from src.sourcing import reddit

    try:
        posts = reddit.top_videos()
    except settings.ConfigError as exc:
        log.error("Reddit non configuré (%s) — ajoute REDDIT_CLIENT_ID/SECRET.", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.error("Reddit: détection échouée : %s", exc)
        return 1
    if not posts:
        log.warning("Reddit: aucune vidéo détectée.")
        return 0

    try:
        from src.storage.drive import DriveStorage

        settings.validate(["drive"])
        storage = DriveStorage.from_settings()
        state = storage.load_state()
        root = storage.named_root(settings.REDDIT_DRIVE_FOLDER_NAME)
    except Exception as exc:  # noqa: BLE001
        log.error("Storage Drive indisponible (%s) — cycle interrompu.", exc)
        return 1

    produced: list[dict] = []
    for post in posts:
        if len(produced) >= settings.REDDIT_PER_RUN:
            break
        if state.is_processed(post.uid):
            continue
        try:
            raw = reddit.download(post, settings.DOWNLOAD_DIR)
        except Exception as exc:  # noqa: BLE001
            log.warning("Reddit: download %s KO : %s", post.uid, exc)
            continue

        name = f"{_slug(post.title) or 'clip'}_{post.id}.mp4"
        out = settings.CLIPS_DIR / name
        try:
            make_vertical_clip(raw, 0.0, settings.REDDIT_CLIP_MAX_S, out)
        except Exception as exc:  # noqa: BLE001
            log.error("Reddit: montage %s KO : %s", post.uid, exc)
            continue

        if not settings.DRY_RUN:
            try:
                storage.upload(out, name, subdir=settings.DRIVE_SUBDIR_CLIPS, root=root)
            except Exception as exc:  # noqa: BLE001
                log.warning("Reddit: upload %s KO : %s", name, exc)
        produced.append({"clip": name, "title": post.title, "sub": post.subreddit,
                         "ups": post.ups, "url": post.permalink})
        state.mark_processed(post.uid)
        storage.save_state(state)
        log.info("Reddit: ✅ %s (r/%s, %d ups)", name, post.subreddit, post.ups)

    if not produced:
        log.warning("Reddit: 0 clip produit ce cycle.")
        return 0
    if settings.DRY_RUN:
        return 0

    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        try:
            storage.cleanup_old(settings.DRIVE_SUBDIR_CLIPS, settings.DRIVE_CLEANUP_MAX_AGE_H,
                                root=root, hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE)
        except Exception as exc:  # noqa: BLE001
            log.warning("Reddit: nettoyage échoué : %s", exc)

    if settings.NOTIFY_EMAIL_ENABLE:
        try:
            from src.notify import _folder_url, send_email

            url = _folder_url(root)
            items = "".join(
                f"<li><b>{c['title'][:80]}</b><br><span style='color:#555'>r/{c['sub']} · "
                f"{c['ups']} ups · <code>{c['clip']}</code></span></li>" for c in produced
            )
            html = (
                "<div style='font-family:system-ui,Arial,sans-serif;font-size:15px'>"
                f"<h2>{len(produced)} clip(s) cute/animaux prêt(s)</h2>"
                f"<p><a href='{url}'>Ouvrir le dossier Drive</a></p><ul>{items}</ul>"
                "<p style='color:#888;font-size:13px'>Poste sur ta chaîne ciblée Thaïlande + "
                "ajoute un son tendance local. Pense à créditer le créateur d'origine.</p></div>"
            )
            send_email(f"🐾 clipping-bot : {len(produced)} clip(s) cute prêt(s)", html,
                       f"{len(produced)} clips cute prêts\n{url}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Reddit: notification e-mail échouée : %s", exc)

    log.info("=== Cycle Reddit terminé : %d clip(s) ===", len(produced))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
