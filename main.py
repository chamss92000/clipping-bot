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
import re
import sys
import unicodedata

from config import settings
from src.detection import detect_all, market_of
from src.utils.logging import attach_file_handler, get_logger

log = get_logger("main")


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


def _process_source(
    cand, storage, state, budget: int | None = None,
    market: str = "intl", clip_root: str | None = None,
) -> int:
    """Traite une source de bout en bout. Retourne le nb de clips produits.

    `budget` = nombre max de clips à produire pour cette source (reste du
    quota du cycle). None => settings.MAX_CLIPS_PER_RUN.
    `market` = "fr" | "intl" (langue du hook) ; `clip_root` = dossier Drive
    racine où déposer les clips de ce marché (None => dossier principal).
    """
    if budget is None:
        budget = settings.MAX_CLIPS_PER_RUN
    # Imports tardifs : n'importe torch/mediapipe que si on traite réellement.
    from src.downloader.download import download, download_planned
    from src.editing.clip import make_vertical_clip
    from src.editing.subtitles import build_ass, burn_subtitles
    from src.publisher.tiktok import build_caption, next_publish_slot, publish
    from src.transcription.whisper_transcribe import transcribe
    from src.viral.gemini_detect import ViralMoment, detect_moments, generate_caption

    is_clip = bool(cand.extra.get("is_clip"))
    produced = 0
    if is_clip:
        # Clip déjà viral : on télécharge l'intégralité (court), pas d'échantillonnage.
        try:
            downloads = [download(cand, settings.DOWNLOAD_DIR)]
        except Exception as exc:  # noqa: BLE001
            log.warning("  %s : téléchargement du clip échoué : %s", cand.uid, exc)
            return 0
    else:
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

        words = [w for s in transcript.segments for w in s.words]

        if is_clip:
            # Le clip EST le moment viral : on prend toute sa durée, et on
            # génère juste une accroche (Gemini) à partir du titre + transcript.
            end = min(dl.duration_s, settings.CLIP_MAX_DURATION_S)
            hook, hashtags = generate_caption(transcript, cand.title, lang=market)
            moments = [ViralMoment(start=0.0, end=end, score=cand.score, hook=hook, hashtags=hashtags)]
        else:
            if not transcript.segments:
                log.info("  %s : transcript vide, fenêtre ignorée.", cand.uid)
                continue
            if len(words) < settings.MIN_WORDS_PER_WINDOW:
                log.info(
                    "  Trop peu de parole (%d mots < %d) — fenêtre ignorée (musique/gameplay muet).",
                    len(words), settings.MIN_WORDS_PER_WINDOW,
                )
                continue
            moments = detect_moments(transcript, dl.duration_s + offset, lang=market)

        for i, m in enumerate(moments):
            if produced >= budget:
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
            # id interne (ascii, stable) pour les fichiers intermédiaires
            base = f"{cand.platform.value}_{cand.source_id}_{int(m.start)}"
            raw_clip = settings.CLIPS_DIR / f"{base}.mp4"
            # nom FINAL lisible : slug du hook + court id source (point 4)
            pretty = f"{_slugify(m.hook or cand.title)}_{cand.source_id[:6]}"
            final_clip = settings.CLIPS_DIR / f"{pretty}.mp4"

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
                # Mode semi-auto : on dépose UNIQUEMENT le clip (.mp4) dans le
                # dossier Drive du marché (=> chaîne fr / chaîne intl). Le hook et
                # les hashtags sont regroupés dans le rapport (pas de .txt épars).
                try:
                    storage.upload(
                        final_clip, final_clip.name,
                        subdir=settings.DRIVE_SUBDIR_CLIPS, root=clip_root,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("  Upload Drive échoué : %s", exc)
                log.info("  ✅ Clip [%s] prêt : %s", market, final_clip.name)

                # Publication auto YouTube Shorts (désactivée par défaut).
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
                        "market": market,
                        "hook": m.hook,
                        "hashtags": m.hashtags,
                        "caption": caption,
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

    # --- Dossiers Drive par marché (2 chaînes) ------------------------------
    # intl => dossier principal existant ; fr => dossier dédié (créé au besoin).
    market_roots: dict[str, str | None] = {"intl": None}
    if "fr" in settings.MARKETS:
        try:
            market_roots["fr"] = storage.named_root(settings.DRIVE_ROOT_FOLDER_NAME_FR)
        except Exception as exc:  # noqa: BLE001
            log.warning("Dossier Drive fr indisponible (%s) — fr routé vers le principal.", exc)
            market_roots["fr"] = None

    # --- Ordre de traitement : priorité aux sources RAPIDES/COMPLÈTES --------
    # Une vidéo téléchargeable en entier (courte, ex. YouTube) donne des clips
    # plus cohérents et un run plus rapide/moins cher qu'un VOD de 48h échantillonné.
    def _cost(c) -> tuple[int, float]:
        sampled = c.duration_s is not None and c.duration_s > settings.SOURCE_FULL_MAX_DURATION_S
        return (1 if sampled else 0, -c.score)

    candidates = sorted(candidates, key=_cost)

    # --- Traitement des sources --------------------------------------------
    # QUOTA PAR MARCHÉ : chaque marché (fr / intl) a son propre objectif de
    # clips, indépendant du classement global. Sinon les gros clips anglophones
    # (vélocité de vues énorme) raflent toutes les places et le FR ne sort jamais.
    targets = {mk: settings.CLIPS_PER_MARKET for mk in settings.MARKETS}
    produced_by_market = {mk: 0 for mk in settings.MARKETS}
    attempts = 0
    clips_total = 0
    published_before = len(state.published)
    chosen: list = []
    #: Plateformes qui bloquent (anti-bot) : abandonnées pour tout ce cycle.
    blocked_platforms: set = set()
    for cand in candidates:
        if all(produced_by_market[mk] >= targets[mk] for mk in targets):
            log.info("Objectif atteint : %s", dict(produced_by_market))
            break
        if attempts >= settings.MAX_SOURCE_ATTEMPTS_PER_RUN:
            log.warning("Limite d'essais atteinte (%d) — %d clips produits.", attempts, clips_total)
            break
        if state.is_processed(cand.uid):
            log.debug("  %s déjà traité, skip.", cand.uid)
            continue
        if cand.platform in blocked_platforms:
            log.debug("  %s ignoré (plateforme bloquée ce cycle).", cand.uid)
            continue

        market = market_of(cand)
        if market not in settings.MARKETS:
            log.debug("  %s ignoré (marché '%s' désactivé).", cand.uid, market)
            continue
        if produced_by_market[market] >= targets[market]:
            # Ce marché a son compte : on saute (sans consommer d'essai) pour
            # aller chercher un candidat de l'AUTRE marché plus bas dans la liste.
            log.debug("  %s ignoré (marché '%s' déjà complet).", cand.uid, market)
            continue
        clip_root = market_roots.get(market)

        log.info("→ Traitement %s [%s] (%s)", cand.uid, market, cand.title[:60])
        chosen.append(cand)
        attempts += 1
        produced = 0
        platform_blocked = False
        budget = targets[market] - produced_by_market[market]
        try:
            produced = _process_source(
                cand, storage, state, budget=budget, market=market, clip_root=clip_root
            )
        except Exception as exc:  # noqa: BLE001 - isole la panne d'une source
            from src.downloader.download import BotCheckError, is_bot_check

            if isinstance(exc, BotCheckError) or is_bot_check(exc):
                platform_blocked = True
                blocked_platforms.add(cand.platform)
                attempts -= 1  # un blocage plateforme ne "consomme" pas un essai
                log.warning(
                    "  %s bloque (anti-bot) — plateforme abandonnée pour ce cycle, "
                    "on bascule sur les autres.", cand.platform.value,
                )
            else:
                log.error("  Échec source %s : %s", cand.uid, exc, exc_info=True)

        clips_total += produced
        if produced > 0:
            produced_by_market[market] = produced_by_market.get(market, 0) + produced
            # Succès : la source est consommée définitivement.
            state.mark_processed(cand.uid)
        elif platform_blocked:
            # Ce n'est pas la faute de la source : on ne compte aucun échec
            # pour elle, elle reste disponible pour un prochain cycle.
            log.info("  %s reste disponible (blocage plateforme, pas la source).", cand.uid)
        else:
            # Échec (VOD indispo, montage KO…) : souvent temporaire.
            # On ne "grille" pas la source, on la réessaiera au prochain cycle,
            # et on passe immédiatement au candidat suivant.
            n = state.record_failure(cand.uid)
            if n >= settings.MAX_SOURCE_FAILURES:
                log.warning("  %s abandonné après %d échecs.", cand.uid, n)
                state.mark_processed(cand.uid)
            else:
                log.info("  %s : 0 clip (échec %d/%d) — on passe au suivant.",
                         cand.uid, n, settings.MAX_SOURCE_FAILURES)
        storage.save_state(state)  # sauvegarde incrémentale (résilience)

    # --- Nettoyage : purge des clips > N heures (garde de la place) ---------
    if settings.DRIVE_CLEANUP_MAX_AGE_H > 0:
        seen_roots: set = set()
        for mk in ("intl", "fr"):
            if mk not in settings.MARKETS:
                continue
            root = market_roots.get(mk)
            if root in seen_roots:
                continue
            seen_roots.add(root)
            try:
                storage.cleanup_old(
                    settings.DRIVE_SUBDIR_CLIPS,
                    settings.DRIVE_CLEANUP_MAX_AGE_H,
                    root=root,
                    hard_delete=settings.DRIVE_CLEANUP_HARD_DELETE,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Nettoyage Drive (%s) échoué : %s", mk, exc)

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

    # --- Notification e-mail (seulement si de nouveaux clips ont été déposés) --
    new_clips = state.published[published_before:]
    if new_clips and settings.NOTIFY_EMAIL_ENABLE and not settings.DRY_RUN:
        try:
            from src.notify import build_clips_email, send_email

            folder_ids = {"intl": storage._root, "fr": market_roots.get("fr")}
            subject, html, text = build_clips_email(new_clips, folder_ids)
            send_email(subject, html, text)
        except Exception as exc:  # noqa: BLE001 - une notif ratée ne casse rien
            log.warning("Notification e-mail non envoyée : %s", exc)

    # --- Log sur Drive ------------------------------------------------------
    try:
        if log_path.exists():
            storage.upload(log_path, settings.LOG_FILENAME, subdir=settings.DRIVE_SUBDIR_LOGS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Upload du log sur Drive échoué : %s", exc)

    log.info("=== Cycle terminé : %d essais, %d clips produits ===", attempts, clips_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(detect_only="--detect-only" in sys.argv))
