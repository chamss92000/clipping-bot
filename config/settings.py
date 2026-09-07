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
#: Nombre max de candidats retenus par cycle (après agrégation/tri).
MAX_CANDIDATES: int = _get_int("MAX_CANDIDATES", 20)
#: Nombre max de sources RÉELLEMENT traitées par cycle (borne coût CPU/Actions).
MAX_SOURCES_PER_RUN: int = _get_int("MAX_SOURCES_PER_RUN", 2)
#: Nombre max de clips produits/publiés par cycle.
MAX_CLIPS_PER_RUN: int = _get_int("MAX_CLIPS_PER_RUN", 3)
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

UPLOADPOST_API_KEY: str | None = _get("UPLOADPOST_API_KEY")
UPLOADPOST_USER: str | None = _get("UPLOADPOST_USER")


# ---------------------------------------------------------------------------
# Bloc 1 — Détection
# ---------------------------------------------------------------------------
# YouTube
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
KICK_MIN_VIEWERS: int = _get_int("KICK_MIN_VIEWERS", 2000)
KICK_VODS_PER_CHANNEL: int = _get_int("KICK_VODS_PER_CHANNEL", 2)
KICK_FEATURED_LIMIT: int = _get_int("KICK_FEATURED_LIMIT", 20)


# ---------------------------------------------------------------------------
# Bloc 2 — Téléchargement (yt-dlp)
# ---------------------------------------------------------------------------
#: Résolution max téléchargée (on n'a pas besoin de 4K pour un rendu 1080x1920).
DOWNLOAD_MAX_HEIGHT: int = _get_int("DOWNLOAD_MAX_HEIGHT", 1080)
#: Nombre de tentatives yt-dlp internes (indépendant de notre décorateur retry).
DOWNLOAD_RETRIES: int = _get_int("DOWNLOAD_RETRIES", 3)
#: Téléchargements de fragments HLS/DASH en parallèle (VODs Twitch/Kick).
DOWNLOAD_CONCURRENCY: int = _get_int("DOWNLOAD_CONCURRENCY", 4)

# --- Stratégie "longs VODs" (subathons de 20-40h impossibles à traiter en free tier) ---
#: VOD de durée <= ce seuil => on télécharge/traite l'intégralité.
SOURCE_FULL_MAX_DURATION_S: int = _get_int("SOURCE_FULL_MAX_DURATION_S", 5400)  # 90 min
#: Au-delà, on échantillonne N fenêtres réparties dans le VOD.
SAMPLE_WINDOWS: int = _get_int("SAMPLE_WINDOWS", 3)
SAMPLE_WINDOW_S: int = _get_int("SAMPLE_WINDOW_S", 1200)  # 20 min
#: On saute le début (mise en route, écran d'attente, pub) et la toute fin.
SAMPLE_SKIP_INTRO_S: int = _get_int("SAMPLE_SKIP_INTRO_S", 300)
SAMPLE_SKIP_OUTRO_S: int = _get_int("SAMPLE_SKIP_OUTRO_S", 180)
#: Garde-fou absolu : on ignore purement un VOD au-delà (0 = pas de limite).
SOURCE_HARD_MAX_DURATION_S: int = _get_int("SOURCE_HARD_MAX_DURATION_S", 0)


# ---------------------------------------------------------------------------
# Bloc 3 — Transcription (Whisper)
# ---------------------------------------------------------------------------
WHISPER_MODEL: str = _get("WHISPER_MODEL", "base")
WHISPER_LANGUAGE: str | None = _get("WHISPER_LANGUAGE")  # None => autodetect
WHISPER_DEVICE: str = _get("WHISPER_DEVICE", "cpu")


# ---------------------------------------------------------------------------
# Bloc 4 — Détection moments viraux (Gemini)
# ---------------------------------------------------------------------------
# gemini-1.5/2.5-flash retirés/fermés aux nouveaux comptes. On utilise l'alias
# `gemini-flash-latest` (toujours le flash courant) pour éviter la valse des
# versions retirées. Surchargeable via env (ex. gemini-3.6-flash).
GEMINI_MODEL: str = _get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_MAX_CLIPS: int = _get_int("GEMINI_MAX_CLIPS", 5)
GEMINI_MIN_CLIPS: int = _get_int("GEMINI_MIN_CLIPS", 3)
#: Fenêtre de durée acceptable d'un clip TikTok (secondes).
CLIP_MIN_DURATION_S: int = _get_int("CLIP_MIN_DURATION_S", 15)
CLIP_MAX_DURATION_S: int = _get_int("CLIP_MAX_DURATION_S", 75)
#: Rate limit free tier Gemini 1.5 Flash : 15 req/min => ~4s d'espacement.
GEMINI_MIN_INTERVAL_S: float = _get_float("GEMINI_MIN_INTERVAL_S", 4.0)


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
FFMPEG_BIN: str = _get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN: str = _get("FFPROBE_BIN", "ffprobe")


# ---------------------------------------------------------------------------
# Bloc 6 — Sous-titres
# ---------------------------------------------------------------------------
SUB_FONT: str = _get("SUB_FONT", "Montserrat")
SUB_FONT_SIZE: int = _get_int("SUB_FONT_SIZE", 64)
SUB_PRIMARY_COLOR: str = _get("SUB_PRIMARY_COLOR", "&H00FFFFFF")  # blanc (ASS BGR)
SUB_HIGHLIGHT_COLOR: str = _get("SUB_HIGHLIGHT_COLOR", "&H0000F0FF")  # jaune
SUB_OUTLINE_COLOR: str = _get("SUB_OUTLINE_COLOR", "&H00000000")  # noir
SUB_MAX_WORDS_PER_LINE: int = _get_int("SUB_MAX_WORDS_PER_LINE", 4)


# ---------------------------------------------------------------------------
# Bloc 7 — Publication TikTok (API officielle Content Posting — gratuite)
# ---------------------------------------------------------------------------
# Mode de publication :
#   "manual"     -> le pipeline dépose clip + caption sur Drive, tu postes à la main
#                   (0€, aucun domaine/audit TikTok requis) — DÉFAUT.
#   "tiktok_api" -> publication auto via l'API officielle TikTok (nécessite
#                   domaine vérifié + audit pour le public ; voir authorize_tiktok).
PUBLISH_MODE: str = _get("PUBLISH_MODE", "manual")

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
