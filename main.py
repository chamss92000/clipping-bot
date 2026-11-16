"""Orchestrateur du pipeline de clipping gaming (focus GTA 6).

Point d'entrée unique lancé par GitHub Actions (cron) ET en local.
Pour chaque clip viral détecté (Twitch/Kick, jeux suivis) :

    téléchargement → brief LLM multimodal (note, recadrage, titre, légende)
    → [note trop basse : clip écarté] → transcription (sous-titres)
    → recadrage 9:16 + sous-titres + titre → dépôt Drive (+ publication)

État persistant sur Drive (state.json) : déduplication des clips déjà traités,
historique des titres (anti-répétition), quotas de publication.

Usage :
    python main.py                 # cycle complet
    python main.py --detect-only   # s'arrête après la détection (dump local)
    DRY_RUN=true python main.py    # tout sauf la publication réelle
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata

from config import settings
from src.detection import detect_all, market_of
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("main")

#: Langues en alphabet latin : les seules où les sous-titres mot-à-mot s'affichent bien.
_LATIN_LANGS = {"fr", "en", "es", "de", "it", "pt", "nl", "pl", "tr", "sv", "da", "no",
                "fi", "ro", "cs", "hu", "id", "ms", "tl", "ca"}


def _slugify(text: str, max_len: int = 48) -> str:
    """Titre -> nom de fichier lisible et sûr (ascii, tirets)."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-") or "clip"


def _dump_candidates(candidates) -> None:
    out = settings.WORK_DIR / "candidates.json"
    out.write_text(
        json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Candidats écrits dans %s", out)


def _process_clip(cand, storage, state, market: str, clip_root: str | None) -> tuple[str, dict]:
    """Traite un clip de bout en bout.

    Retourne (statut, infos) avec statut ∈ {"ok", "rejected", "failed"}.
    Lève BotCheckError si la plateforme bloque le téléchargement.
    """
    # Imports tardifs : n'importe torch/mediapipe que si on traite réellement.
    from src.downloader.download import download
    from src.editing import clip as clip_mod
    from src.editing.clip import make_vertical_clip
    from src.editing.subtitles import build_ass, burn_subtitles
    from src.editing.title_card import render_title, title_center_y
    from src.publisher.tiktok import build_caption, next_publish_slot, publish
    from src.transcription.whisper_transcribe import transcribe
    from src.viral.gemini import analyze_clip, snap_to_words

    dl = download(cand, settings.DOWNLOAD_DIR)
    game = cand.extra.get("game_name") or ""
    tmp_files = [dl.path]
    try:
        # --- Brief LLM : note + recadrage + titre (AVANT Whisper : un clip
        # rejeté ne coûte pas de transcription) -------------------------------
        recent = [p.get("hook", "") for p in state.published if p.get("market") == market][-15:]
        brief = analyze_clip(
            dl.path, dl.duration_s,
            title=cand.title, creator=cand.creator, game=game,
            platform=cand.platform.value, views=cand.views,
            lang=market, recent_hooks=recent,
        )
        info = {"brief": brief.to_dict()}
        if brief.score is not None and brief.score < settings.CLIP_MIN_SCORE:
            log.info("  ✗ Clip écarté par le LLM (note %.0f < %.0f) : %s",
                     brief.score, settings.CLIP_MIN_SCORE, brief.what_happens[:120])
            return "rejected", info

        # --- Sous-titres -----------------------------------------------------
        words = []
        try:
            transcript = transcribe(dl.path, language=cand.extra.get("language"))
            if (transcript.language or "en")[:2] in _LATIN_LANGS:
                words = [w for s in transcript.segments for w in s.words]
            else:
                log.info("  Langue '%s' non latine : pas de sous-titres.", transcript.language)
        except Exception as exc:  # noqa: BLE001 - un clip sans sous-titres reste publiable
            log.warning("  Transcription échouée (%s) — clip sans sous-titres.", exc)

        start, end = snap_to_words(brief.start, brief.end, words, dl.duration_s)
        if end - start < 1.0:
            start, end = 0.0, min(dl.duration_s, float(settings.CLIP_MAX_DURATION_S))

        # --- Montage ---------------------------------------------------------
        base = f"{cand.platform.value}_{re.sub(r'[^A-Za-z0-9_-]', '', cand.source_id)[:40]}"
        raw_clip = settings.CLIPS_DIR / f"{base}.mp4"
        short_id = re.sub(r"\W", "", cand.source_id)[-6:]
        final_clip = settings.CLIPS_DIR / f"{_slugify(brief.hook or cand.title)}_{short_id}.mp4"
        tmp_files.append(raw_clip)
        try:
            make_vertical_clip(dl.path, start, end, raw_clip)
            ass = build_ass(
                words, settings.SUBS_DIR / f"{base}.ass",
                clip_offset=start, clip_duration=end - start,
            )
            title_png, title_y = None, None
            if settings.TITLE_ENABLE and brief.hook:
                title_png = render_title(brief.hook, settings.SUBS_DIR / f"{base}_title.png")
                from PIL import Image

                with Image.open(title_png) as _t:
                    title_h = _t.height
                title_y = title_center_y(clip_mod.LAST_MODE, title_h, clip_mod.LAST_FACE_OUT)
            burn_subtitles(raw_clip, ass, final_clip, title_png=title_png, title_center_y=title_y)
        except Exception as exc:  # noqa: BLE001
            log.error("  Montage échoué (%s) : %s", cand.uid, exc)
            return "failed", info

        caption = build_caption(brief.hook, brief.hashtags, brief.description)
        record = {
            "uid": cand.uid,
            "clip": final_clip.name,
            "market": market,
            "game": game,
            "creator": cand.creator,
            "source_url": cand.url,
            "hook": brief.hook,
            "alternatives": brief.alternatives,
            "description": brief.description,
            "hashtags": brief.hashtags,
            "caption": caption,
            "score": brief.score,
            "model": brief.model,
        }

        # --- Livraison -------------------------------------------------------
        if settings.PUBLISH_MODE == "manual":
            try:
                storage.upload(final_clip, final_clip.name, subdir=settings.DRIVE_SUBDIR_CLIPS, root=clip_root)
            except Exception as exc:  # noqa: BLE001
                log.warning("  Upload Drive échoué : %s", exc)
            record["status"] = "ready_manual"
            if settings.YOUTUBE_UPLOAD_ENABLE and state.youtube_left() > 0:
                try:
                    from src.publisher.youtube import upload_short

                    yt = upload_short(final_clip, brief.hook, brief.hashtags, creator=cand.creator)
                    record["youtube"] = yt.status
                    if yt.status == "posted":
                        state.record_youtube_upload()
                except Exception as exc:  # noqa: BLE001
                    log.warning("  Upload YouTube échoué : %s", exc)
                    record["youtube"] = "failed"
        else:
            try:
                storage.upload(final_clip, final_clip.name, subdir=settings.DRIVE_SUBDIR_CLIPS, root=clip_root)
            except Exception as exc:  # noqa: BLE001
                log.warning("  Upload Drive du clip échoué : %s", exc)
            res = publish(final_clip, caption, [], schedule_at=next_publish_slot())
            record.update(status=res.status, post_id=res.post_id)
            if res.status in ("posted", "scheduled"):
                state.record_upload()

        state.published.append(record)
        log.info("  ✅ Clip [%s] prêt : %s — « %s »", market, final_clip.name, brief.hook)
        tmp_files.append(final_clip)
        return "ok", info
    finally:
        # Disque du runner limité : on ne garde rien une fois le clip livré.
        for f in tmp_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def run(detect_only: bool = False) -> int:
    log.info("=== Démarrage cycle clipping-bot (DRY_RUN=%s) ===", settings.DRY_RUN)
    log.info("Jeux : prioritaires=%s | secondaires=%s",
             settings.GAMES_PRIMARY, settings.GAMES_SECONDARY)
    log_path = settings.WORK_DIR / settings.LOG_FILENAME
    attach_file_handler(log_path)

    # --- Bloc 1 : détection -------------------------------------------------
    candidates = detect_all()
    if not candidates:
        log.warning("Aucun clip candidat détecté — fin du cycle.")
        return 0
    _dump_candidates(candidates)
    if detect_only:
        log.info("--detect-only : arrêt après détection (%d candidats).", len(candidates))
        return 0

    # --- Storage / état -----------------------------------------------------
    try:
        from src.storage.drive import DriveStorage

        settings.validate(["drive"])
        storage = DriveStorage.from_settings()
        state = storage.load_state()
    except Exception as exc:  # noqa: BLE001
        log.error("Storage Drive indisponible (%s) — cycle interrompu.", exc)
        return 1

    # --- Dossiers Drive par marché (2 chaînes) ------------------------------
    market_roots: dict[str, str | None] = {"intl": None}
    if "fr" in settings.MARKETS:
        try:
            market_roots["fr"] = storage.named_root(settings.DRIVE_ROOT_FOLDER_NAME_FR)
        except Exception as exc:  # noqa: BLE001
            log.warning("Dossier Drive fr indisponible (%s) — fr routé vers le principal.", exc)
            market_roots["fr"] = None

    # --- Traitement : quota PAR MARCHÉ (sinon l'anglais rafle tout) ----------
    from src.downloader.download import BotCheckError, is_bot_check

    targets = {mk: settings.CLIPS_PER_MARKET for mk in settings.MARKETS}
    produced = {mk: 0 for mk in settings.MARKETS}
    attempts = 0
    published_before = len(state.published)
    chosen: list = []
    llm_rejected: list[tuple] = []
    blocked_platforms: set = set()
    for cand in candidates:
        total = sum(produced.values())
        if all(produced[mk] >= targets[mk] for mk in targets) or total >= settings.MAX_CLIPS_PER_RUN:
            log.info("Objectif atteint : %s", produced)
            break
        if attempts >= settings.MAX_SOURCE_ATTEMPTS_PER_RUN:
            log.warning("Limite d'essais atteinte (%d) — %d clips produits.", attempts, total)
            break
        if state.is_processed(cand.uid) or cand.platform in blocked_platforms:
            continue
        market = market_of(cand)
        if market not in targets or produced[market] >= targets[market]:
            continue

        log.info("→ %s [%s] %s · %s — %s", cand.uid, market, cand.extra.get("game_name"),
                 cand.creator, cand.title[:60])
        chosen.append(cand)
        attempts += 1
        status = "failed"
        try:
            status, info = _process_clip(cand, storage, state, market, market_roots.get(market))
        except Exception as exc:  # noqa: BLE001 - isole la panne d'un clip
            if isinstance(exc, BotCheckError) or is_bot_check(exc):
                blocked_platforms.add(cand.platform)
                attempts -= 1
                status = "blocked"
                log.warning("  %s bloque (anti-bot) — plateforme abandonnée pour ce cycle.",
                            cand.platform.value)
            else:
                log.error("  Échec %s : %s", cand.uid, exc, exc_info=True)

        if status == "ok":
            produced[market] += 1
            state.mark_processed(cand.uid)
        elif status == "rejected":
            state.mark_processed(cand.uid)  # jugé faible : inutile de le revoir
            llm_rejected.append((cand, info["brief"]))
        elif status == "failed":
            n = state.record_failure(cand.uid)
            if n >= settings.MAX_SOURCE_FAILURES:
                state.mark_processed(cand.uid)
        storage.save_state(state)  # sauvegarde incrémentale (résilience)

    # --- Nettoyage : purge des clips > N heures (garde de la place) ---------
    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        for root in {market_roots.get(mk) for mk in settings.MARKETS}:
            try:
                storage.cleanup_old(
                    settings.DRIVE_SUBDIR_CLIPS, settings.DRIVE_CLEANUP_MAX_AGE_H,
                    root=root, hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Nettoyage Drive échoué : %s", exc)

    new_clips = state.published[published_before:]

    # --- Rapport de run sur Drive -------------------------------------------
    try:
        from src.detection import LAST_REJECTED
        from src.report import build_report

        report = build_report(
            candidates=candidates, rejected=LAST_REJECTED, chosen=chosen,
            clips=new_clips, llm_rejected=llm_rejected,
        )
        report_path = settings.WORK_DIR / "rapport.md"
        report_path.write_text(report, encoding="utf-8")
        storage.upload(report_path, "rapport.md")
        log.info("Rapport déposé sur Drive : rapport.md")
    except Exception as exc:  # noqa: BLE001
        log.warning("Rapport non généré : %s", exc)

    # --- Notification e-mail (seulement si de nouveaux clips ont été déposés) --
    if new_clips and settings.NOTIFY_EMAIL_ENABLE and not settings.DRY_RUN:
        try:
            from src.notify import build_clips_email, send_email

            folder_ids = {"intl": storage._root, "fr": market_roots.get("fr")}
            subject, html, text = build_clips_email(new_clips, folder_ids)
            send_email(subject, html, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification e-mail non envoyée : %s", exc)

    try:
        if log_path.exists():
            storage.upload(log_path, settings.LOG_FILENAME, subdir=settings.DRIVE_SUBDIR_LOGS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Upload du log sur Drive échoué : %s", exc)

    log.info("=== Cycle terminé : %d essais, %d clips produits, %d écartés par le LLM ===",
             attempts, len(new_clips), len(llm_rejected))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(detect_only="--detect-only" in sys.argv))
