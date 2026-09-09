"""Configuration centrale du clipping-bot.

Ce module :
  * charge un éventuel fichier `.env` en dev local (no-op en CI) ;
  * expose des helpers de lecture d'environnement typés ;
  * définit toutes les constantes de tuning de chaque bloc ;
  * fournit `validate(required_blocks=...)` pour échouer tôt et clairement
    si une clé indispensable manque.

Aucune valeur secrète n'est jamais loggée. Les secrets ne sont lus qu'ici.

Convention : chaque bloc du pipeline déclare les secrets/env dont il a besoin
dans `REQUIRED_BY_BLOCK` afin qu'on puisse valider *seulement* ce qu'on va
exécuter (utile pour tester le Bloc 1 sans avoir encore les clés Gemini/Drive).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Chargement .env (dev local uniquement — en CI les vars viennent des secrets)
# ---------------------------------------------------------------------------
try:  # python-dotenv est optionnel à l'exécution CI
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dépendance absente => on ignore
    pass


# ---------------------------------------------------------------------------
# Helpers de lecture d'environnement
# ---------------------------------------------------------------------------
class ConfigError(RuntimeError):
    """Levée quand une configuration requise est absente ou invalide."""


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier, reçu={raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un float, reçu={raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = _get(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Chemins locaux (espace de travail éphémère sur le runner)
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
WORK_DIR: Path = Path(_get("WORK_DIR", str(PROJECT_ROOT / "work")))
DOWNLOAD_DIR: Path = WORK_DIR / "downloads"
CLIPS_DIR: Path = WORK_DIR / "clips"
SUBS_DIR: Path = WORK_DIR / "subs"

for _d in (WORK_DIR, DOWNLOAD_DIR, CLIPS_DIR, SUBS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------------
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO").upper()
DRY_RUN: bool = _get_bool("DRY_RUN", False)
#: Nombre max de candidats retenus par cycle (après agrégation/tri). Assez large
#: pour que des sources FR (moins de vues que les gros clips EN) survivent au tri.
MAX_CANDIDATES: int = _get_int("MAX_CANDIDATES", 80)
#: Nombre max de sources ayant RÉELLEMENT produit des clips par cycle.
MAX_SOURCES_PER_RUN: int = _get_int("MAX_SOURCES_PER_RUN", 1)
#: Nombre max de sources ESSAYÉES par cycle : si une source échoue (blocage
#: anti-bot, VOD indisponible…), on passe à la suivante au lieu d'arrêter.
MAX_SOURCE_ATTEMPTS_PER_RUN: int = _get_int("MAX_SOURCE_ATTEMPTS_PER_RUN", 12)
#: Une source n'est définitivement abandonnée qu'après ce nombre d'échecs.
MAX_SOURCE_FAILURES: int = _get_int("MAX_SOURCE_FAILURES", 3)
#: Nombre max de clips produits/publiés par cycle (toutes chaînes confondues).
MAX_CLIPS_PER_RUN: int = _get_int("MAX_CLIPS_PER_RUN", 4)
#: Objectif de clips PAR MARCHÉ (fr / intl). Chaque chaîne a sa place réservée,
#: indépendamment du classement (sinon les clips EN raflent tout et le FR ne
#: sort jamais). Total réel = CLIPS_PER_MARKET × nombre de marchés actifs.
CLIPS_PER_MARKET: int = _get_int("CLIPS_PER_MARKET", 2)
#: Fuseau utilisé pour la planification des publications.
TIMEZONE: str = _get("TIMEZONE", "Europe/Paris")

# Politique de retry par défaut (utilisée par utils.retry)
RETRY_ATTEMPTS: int = _get_int("RETRY_ATTEMPTS", 4)
RETRY_BASE_DELAY: float = _get_float("RETRY_BASE_DELAY", 1.5)
RETRY_MAX_DELAY: float = _get_float("RETRY_MAX_DELAY", 30.0)
HTTP_TIMEOUT: int = _get_int("HTTP_TIMEOUT", 20)


# ---------------------------------------------------------------------------
# Secrets / clés API
# ---------------------------------------------------------------------------
YOUTUBE_API_KEY: str | None = _get("YOUTUBE_API_KEY")

TWITCH_CLIENT_ID: str | None = _get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET: str | None = _get("TWITCH_CLIENT_SECRET")

GEMINI_API_KEY: str | None = _get("GEMINI_API_KEY")

# --- Drive OAuth utilisateur (méthode budget 0€ : fichiers possédés par TOI) ---
# En CI : les 3 valeurs ci-dessous suffisent (headless via refresh token).
GOOGLE_OAUTH_CLIENT_ID: str | None = _get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET: str | None = _get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REFRESH_TOKEN: str | None = _get("GOOGLE_OAUTH_REFRESH_TOKEN")
# En local : fichier client OAuth téléchargé + token.json produit par `authorize`.
GOOGLE_OAUTH_CLIENT_FILE: str | None = _get("GOOGLE_OAUTH_CLIENT_FILE", "./oauth_client.json")
GOOGLE_OAUTH_TOKEN_FILE: str | None = _get("GOOGLE_OAUTH_TOKEN_FILE", "./token.json")

# --- Drive Service Account (fallback : ne marche QU'AVEC un Shared Drive Workspace) ---
GOOGLE_DRIVE_CREDENTIALS: str | None = _get("GOOGLE_DRIVE_CREDENTIALS")
GOOGLE_DRIVE_CREDENTIALS_FILE: str | None = _get("GOOGLE_DRIVE_CREDENTIALS_FILE")

# Dossier racine sur Drive. Optionnel avec OAuth : si absent, un dossier nommé
# DRIVE_ROOT_FOLDER_NAME est créé/retrouvé à la racine de ton Drive.
GOOGLE_DRIVE_FOLDER_ID: str | None = _get("GOOGLE_DRIVE_FOLDER_ID")
DRIVE_ROOT_FOLDER_NAME: str = _get("DRIVE_ROOT_FOLDER_NAME", "clipping-bot")
#: Deux "marchés" => deux dossiers Drive => deux chaînes.
#:  * intl : clips anglophones/internationaux -> dossier racine ci-dessus.
#:  * fr   : clips francophones -> dossier dédié (créé automatiquement).
DRIVE_ROOT_FOLDER_NAME_FR: str = _get("DRIVE_ROOT_FOLDER_NAME_FR", "clipping-bot-fr")
#: Marchés activés. Retire "fr" ou "intl" pour n'en produire qu'un.
MARKETS: list[str] = _get_list("MARKETS", ["fr", "intl"])

# --- Nettoyage automatique du dossier clips (pour garder de la place) ---
#: On purge les clips plus vieux que N heures à chaque cycle (0 = désactivé).
DRIVE_CLEANUP_MAX_AGE_H: int = _get_int("DRIVE_CLEANUP_MAX_AGE_H", 48)
#: True = suppression définitive (récupère la place tout de suite). False = corbeille.
DRIVE_CLEANUP_HARD_DELETE: bool = _get_bool("DRIVE_CLEANUP_HARD_DELETE", True)

UPLOADPOST_API_KEY: str | None = _get("UPLOADPOST_API_KEY")
UPLOADPOST_USER: str | None = _get("UPLOADPOST_USER")


# ---------------------------------------------------------------------------
# Bloc 1 — Détection
# ---------------------------------------------------------------------------
# YouTube
#: YouTube comme SOURCE de clips. DÉSACTIVÉ par défaut : en CI (IP datacenter),
#: yt-dlp est bloqué par l'anti-bot ("Sign in to confirm you're not a bot"), donc
#: on détecte des vidéos qu'on ne peut pas télécharger => essais gaspillés. On
#: s'appuie sur Twitch/Kick (téléchargeables). Réactive-le en local, ou en CI si
#: tu fournis un fichier cookies valide (secret YOUTUBE_COOKIES).
YOUTUBE_SOURCE_ENABLE: bool = _get_bool("YOUTUBE_SOURCE_ENABLE", False)
YOUTUBE_REGION: str = _get("YOUTUBE_REGION", "FR")
#: Catégories YouTube ciblées (IDs officiels).
#: 20=Gaming, 24=Entertainment, 17=Sports, 23=Comedy, 22=People&Blogs.
YOUTUBE_CATEGORY_IDS: list[str] = _get_list("YOUTUBE_CATEGORY_IDS", ["20", "24", "23"])
YOUTUBE_MAX_RESULTS: int = _get_int("YOUTUBE_MAX_RESULTS", 15)
#: On ignore les vidéos trop courtes (Shorts, déjà verticales) ou trop longues.
YOUTUBE_MIN_DURATION_S: int = _get_int("YOUTUBE_MIN_DURATION_S", 120)
YOUTUBE_MAX_DURATION_S: int = _get_int("YOUTUBE_MAX_DURATION_S", 4 * 3600)
YOUTUBE_MIN_VIEWS: int = _get_int("YOUTUBE_MIN_VIEWS", 20_000)

# Twitch
#: Source Twitch : "clips" (moments déjà viraux via l'API Clips — RECOMMANDÉ)
#: ou "vods" (ancien mode : échantillonnage de VODs longues).
TWITCH_SOURCE: str = _get("TWITCH_SOURCE", "clips")
#: Clips : on récupère les plus vus des N derniers jours chez les streamers chauds.
TWITCH_CLIPS_DAYS: int = _get_int("TWITCH_CLIPS_DAYS", 7)
TWITCH_CLIPS_PER_STREAMER: int = _get_int("TWITCH_CLIPS_PER_STREAMER", 8)
TWITCH_CLIP_MIN_VIEWS: int = _get_int("TWITCH_CLIP_MIN_VIEWS", 50)
TWITCH_MIN_VIEWERS: int = _get_int("TWITCH_MIN_VIEWERS", 3000)
TWITCH_TOP_STREAMS: int = _get_int("TWITCH_TOP_STREAMS", 20)
#: Langues de streams retenues (Helix `language`).
TWITCH_LANGUAGES: list[str] = _get_list("TWITCH_LANGUAGES", ["fr", "en"])
#: Nb de VODs récentes récupérées par streamer détecté.
TWITCH_VODS_PER_STREAMER: int = _get_int("TWITCH_VODS_PER_STREAMER", 2)
TWITCH_MIN_VOD_DURATION_S: int = _get_int("TWITCH_MIN_VOD_DURATION_S", 300)

# Kick (API publique non officielle — best-effort, protégé Cloudflare)
#: Slugs de chaînes Kick à surveiller en priorité (fallback si l'endpoint
#: "featured/livestreams" est bloqué).
KICK_CHANNELS: list[str] = _get_list("KICK_CHANNELS", ["xqc", "trainwreckstv", "adin"])
#: Chaînes Kick francophones (leurs clips partent dans le marché "fr").
KICK_FR_CHANNELS: list[str] = _get_list(
    "KICK_FR_CHANNELS", ["kamet0", "amine", "billy"]
)
KICK_MIN_VIEWERS: int = _get_int("KICK_MIN_VIEWERS", 2000)
KICK_VODS_PER_CHANNEL: int = _get_int("KICK_VODS_PER_CHANNEL", 2)
KICK_FEATURED_LIMIT: int = _get_int("KICK_FEATURED_LIMIT", 20)
#: Source Kick : "clips" (moments déjà viraux — RECOMMANDÉ) ou "vods".
KICK_SOURCE: str = _get("KICK_SOURCE", "clips")
KICK_CLIPS_PER_CHANNEL: int = _get_int("KICK_CLIPS_PER_CHANNEL", 8)
KICK_CLIP_MIN_VIEWS: int = _get_int("KICK_CLIP_MIN_VIEWS", 50)
#: Fenêtre de tri des clips Kick : day | week | month | all.
KICK_CLIPS_TIME: str = _get("KICK_CLIPS_TIME", "week")

# --- Filtres qualité de contenu (le plus gros levier sur la qualité des clips) ---
# On écarte le contenu "recyclé"/narration (récaps de films, compilations,
# trailers…) : pas de visage expressif, pas de réaction => clips plats.
# On veut du "talking head" : créateurs/streamers qui réagissent et parlent.
TITLE_BLACKLIST: list[str] = _get_list(
    "TITLE_BLACKLIST",
    [
        # Contenu recyclé / narration (pas de visage, pas de réaction)
        "recap", "récap", "résumé du film", "resume du film", "histoire du film",
        "raconte l'histoire", "explained", "compilation", "best of", "full movie",
        "film complet", "trailer", "bande-annonce", "bande annonce", "lyric",
        "top 10", "top10", "mashup", "edit audio",
        # Retransmissions esport officielles : commentaire technique, aucune
        # facecam, aucune punchline => clips systématiquement plats.
        "bo3", "bo5", "playoff", "qualifier", "group stage", "grand final",
        "championship", "tournament", "esports", "stage 0", "showmatch",
    ],
)
#: Créateurs/chaînes à exclure (sous-chaîne, insensible à la casse).
CHANNEL_BLACKLIST: list[str] = _get_list("CHANNEL_BLACKLIST", [])
#: Une fenêtre avec trop peu de parole (musique/gameplay muet) ne donne rien de
#: bon : ni sous-titres, ni accroche. On la saute.
MIN_WORDS_PER_WINDOW: int = _get_int("MIN_WORDS_PER_WINDOW", 40)


# ---------------------------------------------------------------------------
# Bloc 2 — Téléchargement (yt-dlp)
# ---------------------------------------------------------------------------
#: Résolution max téléchargée (on n'a pas besoin de 4K pour un rendu 1080x1920).
DOWNLOAD_MAX_HEIGHT: int = _get_int("DOWNLOAD_MAX_HEIGHT", 1080)
#: Nombre de tentatives yt-dlp internes (indépendant de notre décorateur retry).
DOWNLOAD_RETRIES: int = _get_int("DOWNLOAD_RETRIES", 3)
#: Téléchargements de fragments HLS/DASH en parallèle (VODs Twitch/Kick).
DOWNLOAD_CONCURRENCY: int = _get_int("DOWNLOAD_CONCURRENCY", 8)
#: Clients de lecture yt-dlp. VIDE = laisser yt-dlp choisir (recommandé).
#: Forcer "tv"/"mweb" casse le téléchargement ("The page needs to be reloaded")
#: sans pour autant contourner l'anti-bot des IP datacenter : seule la solution
#: cookies fonctionne. On garde le réglage disponible mais désactivé.
YTDLP_PLAYER_CLIENTS: list[str] = _get_list("YTDLP_PLAYER_CLIENTS", [])
#: Solution de secours la plus fiable : un fichier cookies YouTube (format
#: Netscape). En CI, le secret YOUTUBE_COOKIES est écrit dans ce fichier.
YOUTUBE_COOKIES_FILE: str | None = _get("YOUTUBE_COOKIES_FILE", "./youtube_cookies.txt")

# --- Stratégie "longs VODs" (subathons de 20-40h impossibles à traiter en free tier) ---
#: VOD de durée <= ce seuil => on télécharge/traite l'intégralité.
#: 20 min : au-delà (VODs Twitch/Kick), on échantillonne (le download Twitch est lent).
SOURCE_FULL_MAX_DURATION_S: int = _get_int("SOURCE_FULL_MAX_DURATION_S", 1200)
#: Au-delà, on échantillonne N fenêtres COURTES réparties dans le VOD.
#: Fenêtres courtes = download/transcription rapides (tient dans le budget Actions).
SAMPLE_WINDOWS: int = _get_int("SAMPLE_WINDOWS", 2)
SAMPLE_WINDOW_S: int = _get_int("SAMPLE_WINDOW_S", 240)  # 4 min
#: On saute le début (mise en route, écran d'attente, pub) et la toute fin.
SAMPLE_SKIP_INTRO_S: int = _get_int("SAMPLE_SKIP_INTRO_S", 300)
SAMPLE_SKIP_OUTRO_S: int = _get_int("SAMPLE_SKIP_OUTRO_S", 180)
#: Garde-fou absolu : on ignore purement un VOD au-delà (0 = pas de limite).
SOURCE_HARD_MAX_DURATION_S: int = _get_int("SOURCE_HARD_MAX_DURATION_S", 0)


# ---------------------------------------------------------------------------
# Bloc 3 — Transcription (Whisper)
# ---------------------------------------------------------------------------
# medium : nettement plus précis que small (surtout en français). Viable car
# les clips sont courts (5-60s) => transcription rapide malgré le modèle plus lourd.
WHISPER_MODEL: str = _get("WHISPER_MODEL", "medium")
WHISPER_LANGUAGE: str | None = _get("WHISPER_LANGUAGE")  # None => autodetect
WHISPER_DEVICE: str = _get("WHISPER_DEVICE", "cpu")
#: Type de calcul faster-whisper : "int8" (rapide/CPU), "int8_float16", "float32".
WHISPER_COMPUTE_TYPE: str = _get("WHISPER_COMPUTE_TYPE", "int8")


# ---------------------------------------------------------------------------
# Bloc 4 — Détection moments viraux (Gemini)
# ---------------------------------------------------------------------------
# flash-lite : quota free tier plus généreux (RPM plus élevé) que flash, et
# largement suffisant pour de l'extraction JSON structurée. Alias "-latest" pour
# éviter la valse des versions retirées. Surchargeable via env.
GEMINI_MODEL: str = _get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_MAX_CLIPS: int = _get_int("GEMINI_MAX_CLIPS", 5)
GEMINI_MIN_CLIPS: int = _get_int("GEMINI_MIN_CLIPS", 3)
#: Fenêtre de durée acceptable d'un clip TikTok (secondes).
CLIP_MIN_DURATION_S: int = _get_int("CLIP_MIN_DURATION_S", 15)
CLIP_MAX_DURATION_S: int = _get_int("CLIP_MAX_DURATION_S", 75)
#: Rate limit free tier Gemini 1.5 Flash : 15 req/min => ~4s d'espacement.
GEMINI_MIN_INTERVAL_S: float = _get_float("GEMINI_MIN_INTERVAL_S", 4.0)
#: Seuil de qualité : on ignore les moments dont le score LLM est sous ce seuil
#: (0-100). Mieux vaut peu de bons clips que beaucoup de moyens.
GEMINI_MIN_SCORE: float = _get_float("GEMINI_MIN_SCORE", 78.0)


# ---------------------------------------------------------------------------
# Bloc 5 — Découpe / recadrage 9:16
# ---------------------------------------------------------------------------
OUTPUT_WIDTH: int = _get_int("OUTPUT_WIDTH", 1080)
OUTPUT_HEIGHT: int = _get_int("OUTPUT_HEIGHT", 1920)
OUTPUT_FPS: int = _get_int("OUTPUT_FPS", 30)
#: Lissage du face-tracking : on ré-échantillonne la position du visage à cette
#: fréquence (Hz) puis on interpole, pour éviter les tremblements de cadre.
FACE_SAMPLE_HZ: float = _get_float("FACE_SAMPLE_HZ", 4.0)
FACE_DETECTION_CONFIDENCE: float = _get_float("FACE_DETECTION_CONFIDENCE", 0.5)
#: Cadrage vertical. "auto" choisit tout seul le meilleur rendu par clip :
#:   * split  : facecam (webcam) détectée dans un coin -> pile facecam en haut +
#:              gameplay en bas (le rendu roi sur TikTok/Shorts pour du gaming).
#:   * face   : gros visage plein cadre (IRL/just chatting) -> crop 9:16 STATIQUE
#:              centré sur le visage (aucun panning : plus de tremblements).
#:   * blur   : pas de visage fiable (gameplay/cinématique) -> image entière + fond flou.
#: Valeurs forçables : "split" | "face" | "blur" (sinon "auto").
FRAMING: str = _get("FRAMING", "auto")
#: En "auto", proportion mini de frames où un visage doit être vu pour ne pas
#: tomber en fond flou.
FACE_MIN_RATE: float = _get_float("FACE_MIN_RATE", 0.35)
#: Hauteur du visage / hauteur source AU-DESSUS de laquelle on considère un
#: "gros visage plein cadre" (talking head) -> crop statique visage.
FACE_BIG_RATIO: float = _get_float("FACE_BIG_RATIO", 0.16)
#: En dessous de FACE_BIG_RATIO mais au-dessus de ce seuil = petite facecam
#: dans un coin -> split-stack. En dessous = détection peu fiable -> fond flou.
FACE_CAM_MIN_RATIO: float = _get_float("FACE_CAM_MIN_RATIO", 0.035)
#: Split-stack : fraction de la hauteur verticale allouée à la facecam (haut).
SPLIT_TOP_FRAC: float = _get_float("SPLIT_TOP_FRAC", 0.42)
#: Facteur d'agrandissement de la boîte visage pour cadrer la facecam (montre
#: le visage + un peu de contexte de la webcam, pas juste le nez).
CAM_ZOOM: float = _get_float("CAM_ZOOM", 3.2)
FFMPEG_BIN: str = _get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN: str = _get("FFPROBE_BIN", "ffprobe")
#: Normalisation de loudness (standard TikTok ~ -14 LUFS) sur le rendu final.
AUDIO_LOUDNORM: bool = _get_bool("AUDIO_LOUDNORM", True)
AUDIO_LOUDNORM_I: float = _get_float("AUDIO_LOUDNORM_I", -14.0)


# ---------------------------------------------------------------------------
# Bloc 6 — Sous-titres
# ---------------------------------------------------------------------------
# Police embarquée dans le repo (assets/fonts) => rendu IDENTIQUE en local et en
# CI. Anton = condensée très grasse, la police des captions virales.
FONTS_DIR: Path = PROJECT_ROOT / "assets" / "fonts"
SUB_FONT: str = _get("SUB_FONT", "Anton")
SUB_FONT_SIZE: int = _get_int("SUB_FONT_SIZE", 92)  # gros = style TikTok
SUB_PRIMARY_COLOR: str = _get("SUB_PRIMARY_COLOR", "&H00FFFFFF")  # blanc (ASS BGR)
SUB_HIGHLIGHT_COLOR: str = _get("SUB_HIGHLIGHT_COLOR", "&H0000F0FF")  # jaune vif
SUB_OUTLINE_COLOR: str = _get("SUB_OUTLINE_COLOR", "&H00000000")  # noir
SUB_OUTLINE_WIDTH: int = _get_int("SUB_OUTLINE_WIDTH", 6)   # contour épais = lisible
SUB_SHADOW: int = _get_int("SUB_SHADOW", 3)
SUB_MARGIN_V: int = _get_int("SUB_MARGIN_V", 620)          # remonte le texte (zone safe TikTok)
SUB_UPPERCASE: bool = _get_bool("SUB_UPPERCASE", True)     # MAJUSCULES = punch
#: Retire la ponctuation en début/fin de chaque mot affiché (rendu plus propre).
SUB_STRIP_PUNCT: bool = _get_bool("SUB_STRIP_PUNCT", True)
SUB_MAX_WORDS_PER_LINE: int = _get_int("SUB_MAX_WORDS_PER_LINE", 3)

# Titre fixe (hook) affiché en haut du clip, toute la durée.
TITLE_ENABLE: bool = _get_bool("TITLE_ENABLE", True)
TITLE_FONT_SIZE: int = _get_int("TITLE_FONT_SIZE", 58)
TITLE_MARGIN_V: int = _get_int("TITLE_MARGIN_V", 150)      # distance depuis le haut
TITLE_UPPERCASE: bool = _get_bool("TITLE_UPPERCASE", False)
TITLE_BOX: bool = _get_bool("TITLE_BOX", True)             # bandeau semi-opaque derrière


# ---------------------------------------------------------------------------
# Bloc 7 — Publication TikTok (API officielle Content Posting — gratuite)
# ---------------------------------------------------------------------------
# Mode de publication :
#   "manual"     -> le pipeline dépose clip + caption sur Drive, tu postes à la main
#                   (0€, aucun domaine/audit TikTok requis) — DÉFAUT.
#   "tiktok_api" -> publication auto via l'API officielle TikTok (nécessite
#                   domaine vérifié + audit pour le public ; voir authorize_tiktok).
PUBLISH_MODE: str = _get("PUBLISH_MODE", "manual")

# --- Publication auto YouTube Shorts (gratuit, public, sans audit) ---
# Active l'upload automatique de chaque clip en Short public sur TA chaîne.
# En plus de la livraison manuelle TikTok (clip + caption sur Drive).
# DÉSACTIVÉ par défaut : la pipeline se contente de déposer les clips sur Drive
# (tu publies toi-même sur tes chaînes). Repasse à true pour ré-activer l'auto-upload.
YOUTUBE_UPLOAD_ENABLE: bool = _get_bool("YOUTUBE_UPLOAD_ENABLE", False)
# OAuth : réutilise le client GOOGLE_OAUTH_CLIENT_ID/SECRET + un refresh token
# dédié au scope youtube.upload (obtenu via `authorize_youtube`).
YOUTUBE_REFRESH_TOKEN: str | None = _get("YOUTUBE_REFRESH_TOKEN")
YOUTUBE_OAUTH_TOKEN_FILE: str | None = _get("YOUTUBE_OAUTH_TOKEN_FILE", "./youtube_token.json")
YOUTUBE_PRIVACY: str = _get("YOUTUBE_PRIVACY", "public")  # public | unlisted | private
YOUTUBE_CATEGORY_ID: str = _get("YOUTUBE_CATEGORY_ID", "20")  # 20 = Gaming
#: Garde-fou quota : l'upload coûte ~1600 unités (quota /jour = 10000 ⇒ ~6 max).
YOUTUBE_DAILY_LIMIT: int = _get_int("YOUTUBE_DAILY_LIMIT", 5)

# Upload-Post free ne permet PAS TikTok => on utilise l'API officielle TikTok.
TIKTOK_CLIENT_KEY: str | None = _get("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET: str | None = _get("TIKTOK_CLIENT_SECRET")
TIKTOK_REFRESH_TOKEN: str | None = _get("TIKTOK_REFRESH_TOKEN")
#: URI de redirection OAuth (doit être EXACTEMENT celle enregistrée dans l'app TikTok).
TIKTOK_REDIRECT_URI: str = _get("TIKTOK_REDIRECT_URI", "https://localhost:8888/callback")
#: Niveau de confidentialité du post. Avant audit de l'app, seul SELF_ONLY (privé)
#: est autorisé ; après audit, passer à PUBLIC_TO_EVERYONE.
TIKTOK_PRIVACY_LEVEL: str = _get("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")
TIKTOK_DISABLE_COMMENT: bool = _get_bool("TIKTOK_DISABLE_COMMENT", False)
TIKTOK_DISABLE_DUET: bool = _get_bool("TIKTOK_DISABLE_DUET", False)
TIKTOK_DISABLE_STITCH: bool = _get_bool("TIKTOK_DISABLE_STITCH", False)
TIKTOK_API_BASE: str = _get("TIKTOK_API_BASE", "https://open.tiktokapis.com")

#: Quota mensuel qu'on s'impose (l'API officielle n'a pas de coût, mais on borde).
PUBLISH_MONTHLY_QUOTA: int = _get_int("PUBLISH_MONTHLY_QUOTA", 60)
#: Heures de pic (heure locale TIMEZONE) où l'on planifie les publications.
PUBLISH_HOURS: list[int] = [int(h) for h in _get_list("PUBLISH_HOURS", ["18", "19", "20", "21", "22"])]
PLATFORM_TARGETS: list[str] = _get_list("PLATFORM_TARGETS", ["tiktok"])


# ---------------------------------------------------------------------------
# Bloc 2 — Storage / state
# ---------------------------------------------------------------------------
STATE_FILENAME: str = _get("STATE_FILENAME", "state.json")
LOG_FILENAME: str = _get("LOG_FILENAME", "pipeline.log")
#: Sous-dossiers logiques créés dans le dossier Drive racine.
DRIVE_SUBDIR_DOWNLOADS: str = "downloads"
DRIVE_SUBDIR_CLIPS: str = "clips"
DRIVE_SUBDIR_LOGS: str = "logs"


# ---------------------------------------------------------------------------
# Validation par bloc
# ---------------------------------------------------------------------------
#: Secrets/env requis pour exécuter chaque bloc. Permet de valider seulement
#: ce dont on a besoin (ex : tester le Bloc 1 sans clés Gemini/Drive).
REQUIRED_BY_BLOCK: dict[str, list[str]] = {
    "youtube": ["YOUTUBE_API_KEY"],
    "twitch": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
    "kick": [],  # scraping public, pas de clé
    "detection": ["YOUTUBE_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
    "download": [],  # credentials Drive gérées par la vérif dédiée ci-dessous
    "drive": [],     # idem (OAuth : GOOGLE_DRIVE_FOLDER_ID est optionnel)
    "viral": ["GEMINI_API_KEY"],
    "publish": ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"],
}


def validate(required_blocks: Iterable[str]) -> None:
    """Vérifie que les secrets requis par `required_blocks` sont présents.

    Lève `ConfigError` avec un message agrégé listant *toutes* les clés
    manquantes (plutôt qu'échouer sur la première), pour un feedback clair.
    """
    missing: list[str] = []
    checked: set[str] = set()
    for block in required_blocks:
        for key in REQUIRED_BY_BLOCK.get(block, []):
            if key in checked:
                continue
            checked.add(key)
            if not globals().get(key):
                missing.append(key)

    # Cas particulier Drive : au moins UNE méthode d'auth doit être disponible.
    if any(b in {"drive", "download"} for b in required_blocks):
        oauth_env = bool(
            GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN
        )
        oauth_file = bool(GOOGLE_OAUTH_TOKEN_FILE and Path(GOOGLE_OAUTH_TOKEN_FILE).exists())
        sa = bool(
            GOOGLE_DRIVE_CREDENTIALS
            or (GOOGLE_DRIVE_CREDENTIALS_FILE and Path(GOOGLE_DRIVE_CREDENTIALS_FILE).exists())
        )
        if not (oauth_env or oauth_file or sa):
            missing.append(
                "auth Drive (OAuth: GOOGLE_OAUTH_REFRESH_TOKEN + CLIENT_ID/SECRET, "
                "ou token.json local ; SA en fallback Shared Drive)"
            )

    if missing:
        raise ConfigError(
            "Configuration incomplète pour les blocs "
            f"{sorted(set(required_blocks))} — variables manquantes : "
            + ", ".join(sorted(set(missing)))
        )
