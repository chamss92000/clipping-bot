"""Orchestrateur du pipeline de clipping (Bloc 8).

Point d'entrée unique lancé par GitHub Actions (cron 6h) ET en local.
Enchaîne : détection → (par source) download → transcription → détection virale
→ (par moment) découpe 9:16 + sous-titres → upload Drive → publication TikTok.

État persistant sur Drive (state.json) : déduplication des sources déjà traitées,
suivi du quota mensuel de publication, historique.

Garde-fous : MAX_SOURCES_PER_RUN, MAX_CLIPS_PER_RUN, quota Upload-Post
(state.uploads_left), DRY_RUN (ne publie rien). Chaque source est isolée :
son échec est loggé sans casser le cycle.

Usage :
    python main.py                 # cycle complet
    python main.py --detect-only   # s'arrête après la détection (dump local)
    DRY_RUN=true python main.py    # tout sauf la publication réelle
"""

from __future__ import annotations

import json
import sys

from config import settings
from src.detection import detect_all
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("main")


def _dump_candidates(candidates) -> None:
    out = settings.WORK_DIR / "candidates.json"
    out.write_text(
        json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Candidats écrits dans %s", out)


def _process_source(cand, storage, state) -> int:
    """Traite une source de bout en bout. Retourne le nb de clips publiés/produits."""
    # Imports tardifs : n'importe torch/mediapipe que si on traite réellement.
    from src.downloader.download import download_planned
    from src.editing.clip import make_vertical_clip
    from src.editing.subtitles import build_ass, burn_subtitles
    from src.publisher.tiktok import build_caption, next_publish_slot, publish
    from src.transcription.whisper_transcribe import transcribe
    from src.viral.gemini_detect import detect_moments

    produced = 0
    downloads = download_planned(cand, settings.DOWNLOAD_DIR)
    if not downloads:
        log.warning("  %s : rien à télécharger (ignoré).", cand.uid)
        return 0

    for dl in downloads:
        offset = dl.window[0] if dl.window else 0.0
        try:
            transcript = transcribe(dl.path, source_offset=offset)
        except Exception as exc:  # noqa: BLE001 - une fenêtre KO n'annule pas les autres
            log.error("  Transcription échouée (%s) : %s", getattr(dl, "path", "?"), exc)
            continue
        if not transcript.segments:
            log.info("  %s : transcript vide, fenêtre ignorée.", cand.uid)
            continue

        words = [w for s in transcript.segments for w in s.words]
        if len(words) < settings.MIN_WORDS_PER_WINDOW:
            log.info(
                "  Trop peu de parole (%d mots < %d) — fenêtre ignorée (musique/gameplay muet).",
                len(words), settings.MIN_WORDS_PER_WINDOW,
            )
            continue

        moments = detect_moments(transcript, dl.duration_s + offset)

        for i, m in enumerate(moments):
            if produced >= settings.MAX_CLIPS_PER_RUN:
                log.info("  Limite MAX_CLIPS_PER_RUN atteinte.")
                return produced
            if (
                settings.PUBLISH_MODE == "tiktok_api"
                and not settings.DRY_RUN
                and state.uploads_left() <= 0
            ):
                log.warning("  Quota mensuel de publication épuisé — arrêt des posts.")
                return produced

            # Référentiels : moments en temps VOD ; le fichier local démarre à `offset`.
            local_start = max(0.0, m.start - offset)
            local_end = m.end - offset
            base = f"{cand.platform.value}_{cand.source_id}_{int(m.start)}"
            raw_clip = settings.CLIPS_DIR / f"{base}.mp4"
            final_clip = settings.CLIPS_DIR / f"{base}_sub.mp4"

            try:
                make_vertical_clip(dl.path, local_start, local_end, raw_clip)
                ass = build_ass(
                    words,
                    settings.SUBS_DIR / f"{base}.ass",
                    clip_offset=m.start,
                    title=m.hook,                       # titre fixe en haut
                    clip_duration=local_end - local_start,
                )
                burn_subtitles(raw_clip, ass, final_clip)
            except Exception as exc:  # noqa: BLE001 - un moment raté n'annule pas les autres
                log.error("  Montage échoué (%s @%.0f) : %s", cand.uid, m.start, exc)
                continue

            caption = build_caption(m.hook, m.hashtags)

            if settings.PUBLISH_MODE == "manual":
                # Mode semi-auto : on dépose le clip + un fichier caption prêt à
                # copier sur Drive ; l'utilisateur publie à la main.
                cap_file = settings.CLIPS_DIR / f"{base}.txt"
                cap_file.write_text(caption, encoding="utf-8")
                try:
                    storage.upload(final_clip, final_clip.name, subdir=settings.DRIVE_SUBDIR_CLIPS)
                    storage.upload(cap_file, cap_file.name, subdir=settings.DRIVE_SUBDIR_CLIPS)
                except Exception as exc:  # noqa: BLE001
                    log.warning("  Upload Drive échoué : %s", exc)
                log.info("  ✅ Clip prêt à publier (manuel TikTok) : %s", final_clip.name)

                # Publication auto YouTube Shorts (en plus du manuel TikTok).
                yt_status = None
                if settings.YOUTUBE_UPLOAD_ENABLE and state.youtube_left() > 0:
                    try:
                        from src.publisher.youtube import upload_short

                        yt = upload_short(final_clip, m.hook, m.hashtags, creator=cand.creator)
                        yt_status = yt.status
                        if yt.status == "posted":
                            state.record_youtube_upload()
                            log.info("  ✅ Short YouTube publié : %s", yt.post_id)
                    except Exception as exc:  # noqa: BLE001 - YT KO n'annule pas le manuel
                        log.warning("  Upload YouTube échoué : %s", exc)
                        yt_status = "failed"
                elif settings.YOUTUBE_UPLOAD_ENABLE:
                    log.info("  (quota YouTube du jour atteint — Short non publié)")

                state.published.append(
                    {
                        "uid": cand.uid,
                        "clip": final_clip.name,
                        "hook": m.hook,
                        "hashtags": m.hashtags,
                        "status": "ready_manual",
                        "youtube": yt_status,
                    }
                )
                produced += 1
                continue

            # Mode API : upload Drive (archive) puis publication automatique.
            try:
                storage.upload(final_clip, final_clip.name, subdir=settings.DRIVE_SUBDIR_CLIPS)
            except Exception as exc:  # noqa: BLE001
                log.warning("  Upload Drive du clip échoué : %s", exc)

            slot = next_publish_slot()
            res = publish(final_clip, m.hook, m.hashtags, schedule_at=slot)
            state.published.append(
                {
                    "uid": cand.uid,
                    "clip": final_clip.name,
                    "hook": m.hook,
                    "status": res.status,
                    "post_id": res.post_id,
                    "scheduled_at": res.scheduled_at.isoformat() if res.scheduled_at else None,
                }
            )
            if res.status in ("posted", "scheduled"):
                state.record_upload()
            produced += 1

    return produced


def run(detect_only: bool = False) -> int:
    log.info("=== Démarrage cycle clipping-bot (DRY_RUN=%s) ===", settings.DRY_RUN)
    log_path = settings.WORK_DIR / settings.LOG_FILENAME
    attach_file_handler(log_path)

    # --- Bloc 1 : détection -------------------------------------------------
    candidates = detect_all()
    if not candidates:
        log.warning("Aucun candidat détecté — fin du cycle.")
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

    # --- Ordre de traitement : priorité aux sources RAPIDES/COMPLÈTES --------
    # Une vidéo téléchargeable en entier (courte, ex. YouTube) donne des clips
    # plus cohérents et un run plus rapide/moins cher qu'un VOD de 48h échantillonné.
    def _cost(c) -> tuple[int, float]:
        sampled = c.duration_s is not None and c.duration_s > settings.SOURCE_FULL_MAX_DURATION_S
        return (1 if sampled else 0, -c.score)

    candidates = sorted(candidates, key=_cost)

    # --- Traitement des sources --------------------------------------------
    sources_done = 0
    clips_total = 0
    published_before = len(state.published)
    chosen: list = []
    for cand in candidates:
        if sources_done >= settings.MAX_SOURCES_PER_RUN:
            log.info("Limite MAX_SOURCES_PER_RUN atteinte.")
            break
        if state.is_processed(cand.uid):
            log.debug("  %s déjà traité, skip.", cand.uid)
            continue
        if not settings.DRY_RUN and state.uploads_left() <= 0:
            log.warning("Quota mensuel épuisé — fin du cycle.")
            break

        log.info("→ Traitement %s (%s)", cand.uid, cand.title[:60])
        chosen.append(cand)
        try:
            clips_total += _process_source(cand, storage, state)
        except Exception as exc:  # noqa: BLE001 - isole la panne d'une source
            log.error("  Échec source %s : %s", cand.uid, exc, exc_info=True)
        finally:
            state.mark_processed(cand.uid)
            storage.save_state(state)  # sauvegarde incrémentale (résilience)
            sources_done += 1

    # --- Rapport de run sur Drive (pour vérifier les sources d'un coup d'œil) --
    try:
        from src.detection import LAST_REJECTED
        from src.report import build_report

        report = build_report(
            candidates=sorted(candidates, key=lambda c: c.score, reverse=True),
            rejected=LAST_REJECTED,
            chosen=chosen,
            clips=state.published[published_before:],
            youtube_left=state.youtube_left(),
        )
        report_path = settings.WORK_DIR / "rapport.md"
        report_path.write_text(report, encoding="utf-8")
        storage.upload(report_path, "rapport.md")
        log.info("Rapport déposé sur Drive : rapport.md")
    except Exception as exc:  # noqa: BLE001
        log.warning("Rapport non généré : %s", exc)

    # --- Log sur Drive ------------------------------------------------------
    try:
        if log_path.exists():
            storage.upload(log_path, settings.LOG_FILENAME, subdir=settings.DRIVE_SUBDIR_LOGS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Upload du log sur Drive échoué : %s", exc)

    log.info("=== Cycle terminé : %d sources traitées, %d clips produits ===", sources_done, clips_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(detect_only="--detect-only" in sys.argv))
