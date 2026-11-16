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
#: Nombre max de clips ESSAYÉS par cycle (téléchargés + notés par le LLM) : un
#: clip refusé (note trop basse, échec réseau…) fait passer au suivant.
MAX_SOURCE_ATTEMPTS_PER_RUN: int = _get_int("MAX_SOURCE_ATTEMPTS_PER_RUN", 20)
#: Une source n'est définitivement abandonnée qu'après ce nombre d'échecs.
MAX_SOURCE_FAILURES: int = _get_int("MAX_SOURCE_FAILURES", 3)
#: Nombre max de clips produits/publiés par cycle (toutes chaînes confondues).
MAX_CLIPS_PER_RUN: int = _get_int("MAX_CLIPS_PER_RUN", 6)
#: Objectif de clips PAR MARCHÉ (fr / intl). Chaque chaîne a sa place réservée,
#: indépendamment du classement (sinon les clips EN raflent tout et le FR ne
#: sort jamais). Total réel = CLIPS_PER_MARKET × nombre de marchés actifs.
CLIPS_PER_MARKET: int = _get_int("CLIPS_PER_MARKET", 3)
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

# --- Notification e-mail (récap quand des clips sont déposés sur Drive) ---
NOTIFY_EMAIL_ENABLE: bool = _get_bool("NOTIFY_EMAIL_ENABLE", False)
NOTIFY_EMAIL_TO: str | None = _get("NOTIFY_EMAIL_TO", "ce.boutajar@gmail.com")
SMTP_HOST: str = _get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = _get_int("SMTP_PORT", 465)  # 465 = SSL, 587 = STARTTLS
SMTP_USER: str | None = _get("SMTP_USER")       # adresse d'envoi (compte Gmail)
SMTP_PASSWORD: str | None = _get("SMTP_PASSWORD")  # mot de passe d'APPLICATION Gmail


# ---------------------------------------------------------------------------
# Bloc 1 — Détection (clips GAMING déjà viraux, focus GTA 6)
# ---------------------------------------------------------------------------
# Un seul type de contenu : des clips Twitch/Kick DÉJÀ viraux (sélectionnés par
# la communauté), filtrés par JEU. On interroge directement l'API Clips de
# Twitch par catégorie de jeu (`/clips?game_id=`) : c'est la source la plus
# fiable et la plus ciblée (tous les streamers du jeu, pas seulement le top live).
#
#: Jeux PRIORITAIRES (nom exact de la catégorie Twitch). Ils reçoivent un bonus
#: de score => ils passent toujours devant les jeux secondaires.
GAMES_PRIMARY: list[str] = _get_list("GAMES_PRIMARY", ["Grand Theft Auto VI"])
#: Jeux SECONDAIRES : utilisés pour compléter le quota quand le prioritaire ne
#: suffit pas (ex. avant la sortie / jours creux). Vide = prioritaires seulement.
GAMES_SECONDARY: list[str] = _get_list("GAMES_SECONDARY", ["Grand Theft Auto V"])
#: Bonus de score (0-100) des jeux prioritaires.
PRIMARY_GAME_BOOST: float = _get_float("PRIMARY_GAME_BOOST", 25.0)

# Twitch
#: Fenêtre de fraîcheur des clips (jours). Court = actualité (sortie GTA 6).
TWITCH_CLIPS_DAYS: int = _get_int("TWITCH_CLIPS_DAYS", 3)
#: Pages de 100 clips lues par jeu (triées par vues décroissantes).
TWITCH_CLIPS_PAGES: int = _get_int("TWITCH_CLIPS_PAGES", 8)
#: Seuil de vues VISÉ. Il est abaissé automatiquement (jusqu'au plancher) quand
#: une langue a moins de TWITCH_MIN_POOL_PER_LANG clips : le LLM tranche ensuite.
TWITCH_CLIP_MIN_VIEWS: int = _get_int("TWITCH_CLIP_MIN_VIEWS", 100)
TWITCH_CLIP_FLOOR_VIEWS: int = _get_int("TWITCH_CLIP_FLOOR_VIEWS", 10)
TWITCH_MIN_POOL_PER_LANG: int = _get_int("TWITCH_MIN_POOL_PER_LANG", 12)
#: Langues retenues (langue de diffusion du streamer).
TWITCH_LANGUAGES: list[str] = _get_list("TWITCH_LANGUAGES", ["fr", "en"])
#: MODE CAMPAGNE (monétisation) : créateurs ciblés en priorité, même hors live.
TARGET_STREAMERS_TWITCH: list[str] = _get_list("TARGET_STREAMERS_TWITCH", [])  # logins
TARGET_CHANNELS_KICK: list[str] = _get_list("TARGET_CHANNELS_KICK", [])         # slugs
#: True = on ne clippe QUE les créateurs ciblés (sinon : ciblés en priorité + jeux).
TARGET_ONLY: bool = _get_bool("TARGET_ONLY", False)
#: True = un créateur ciblé est clippé quel que soit son jeu (sinon : jeux suivis only).
TARGET_ANY_GAME: bool = _get_bool("TARGET_ANY_GAME", False)
TARGET_SCORE_BOOST: float = _get_float("TARGET_SCORE_BOOST", 30.0)

# Diversité / anti-doublons
#: Max de clips gardés PAR STREAMER dans la file (sinon 1 gros streamer rafle tout).
MAX_CLIPS_PER_STREAMER: int = _get_int("MAX_CLIPS_PER_STREAMER", 2)
#: Deux clips du même streamer dont les moments sont à moins de N secondes
#: d'écart dans la VOD = le même moment clippé par 2 viewers => on garde le meilleur.
DUPLICATE_WINDOW_S: int = _get_int("DUPLICATE_WINDOW_S", 45)
#: Durée minimale d'un clip source (trop court = pas d'histoire).
SOURCE_CLIP_MIN_S: int = _get_int("SOURCE_CLIP_MIN_S", 8)

# Kick (API publique non officielle — best-effort, protégé Cloudflare)
#: Chaînes Kick surveillées (on ne garde que leurs clips sur les jeux suivis).
KICK_ENABLE: bool = _get_bool("KICK_ENABLE", True)
KICK_CHANNELS: list[str] = _get_list("KICK_CHANNELS", ["xqc", "trainwreckstv", "adin"])
#: Chaînes Kick francophones (leurs clips partent dans le marché "fr").
KICK_FR_CHANNELS: list[str] = _get_list("KICK_FR_CHANNELS", ["kamet0", "amine", "billy"])
KICK_FEATURED_LIMIT: int = _get_int("KICK_FEATURED_LIMIT", 20)
KICK_CLIPS_PER_CHANNEL: int = _get_int("KICK_CLIPS_PER_CHANNEL", 10)
KICK_CLIP_MIN_VIEWS: int = _get_int("KICK_CLIP_MIN_VIEWS", 100)
#: Fenêtre de tri des clips Kick : day | week | month | all.
KICK_CLIPS_TIME: str = _get("KICK_CLIPS_TIME", "week")

# --- Filtres qualité de contenu ---
#: Titres de clips à écarter : retransmissions officielles / contenu recyclé
#: (pas de réaction, pas de facecam => clips plats).
TITLE_BLACKLIST: list[str] = _get_list(
    "TITLE_BLACKLIST",
    [
        "trailer reupload", "full movie", "compilation", "best of", "lyric",
        "bo3", "bo5", "playoff", "qualifier", "group stage", "grand final",
        "tournament", "esports",
    ],
)
#: Créateurs/chaînes à exclure (sous-chaîne, insensible à la casse).
CHANNEL_BLACKLIST: list[str] = _get_list("CHANNEL_BLACKLIST", [])


# ---------------------------------------------------------------------------
# Bloc 2 — Téléchargement (yt-dlp)
# ---------------------------------------------------------------------------
#: Résolution max téléchargée (on n'a pas besoin de 4K pour un rendu 1080x1920).
DOWNLOAD_MAX_HEIGHT: int = _get_int("DOWNLOAD_MAX_HEIGHT", 1080)
#: Nombre de tentatives yt-dlp internes (indépendant de notre décorateur retry).
DOWNLOAD_RETRIES: int = _get_int("DOWNLOAD_RETRIES", 3)
DOWNLOAD_CONCURRENCY: int = _get_int("DOWNLOAD_CONCURRENCY", 8)


# ---------------------------------------------------------------------------
# Bloc 3 — Transcription (Whisper) => sous-titres
# ---------------------------------------------------------------------------
# large-v3-turbo : précision proche de large-v3 (nettement > medium, surtout en
# français et sur les noms propres) pour une vitesse ~ medium sur CPU (décodeur
# 4 couches). Gratuit, local, 0 quota.
WHISPER_MODEL: str = _get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_LANGUAGE: str | None = _get("WHISPER_LANGUAGE")  # None => langue du clip / autodetect
WHISPER_DEVICE: str = _get("WHISPER_DEVICE", "cpu")
#: Type de calcul faster-whisper : "int8" (rapide/CPU), "int8_float16", "float32".
WHISPER_COMPUTE_TYPE: str = _get("WHISPER_COMPUTE_TYPE", "int8")
#: VAD (Silero) : ne transcrit que la parole. Indispensable en gaming : sinon
#: Whisper "hallucine" des phrases sur la musique/les bruitages du jeu.
WHISPER_VAD: bool = _get_bool("WHISPER_VAD", True)
#: Vocabulaire injecté à Whisper (noms propres bien orthographiés).
WHISPER_HOTWORDS: str = _get(
    "WHISPER_HOTWORDS",
    "GTA, GTA 6, GTA VI, Vice City, Leonida, Lucia, Jason, Rockstar, GTA RP",
)


# ---------------------------------------------------------------------------
# Bloc 4 — Titre / hashtags / note qualité (LLM multimodal gratuit : Gemini)
# ---------------------------------------------------------------------------
# Gemini REGARDE le clip (vidéo basse définition + audio) : il comprend l'action
# de gameplay même sans dialogue, ce qu'un LLM texte seul ne peut pas faire.
# Chaîne de modèles essayés dans l'ordre (repli sur 404 = modèle retiré, ou 429 =
# quota). "-latest" = alias Google qui suit le dernier Flash (pas de maintenance).
GEMINI_MODELS: list[str] = _get_list(
    "GEMINI_MODELS",
    [_get("GEMINI_MODEL") or "gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"],
)
#: Envoie la vidéo du clip au modèle (sinon : titre + transcription seuls).
GEMINI_SEND_VIDEO: bool = _get_bool("GEMINI_SEND_VIDEO", True)
#: Espacement minimal entre deux appels (free tier ~10 req/min sur Flash).
GEMINI_MIN_INTERVAL_S: float = _get_float("GEMINI_MIN_INTERVAL_S", 7.0)
#: Sélection "meilleurs parmi N" : on fait noter N clips par marché puis on ne
#: monte que les CLIPS_PER_MARKET mieux notés (au lieu des premiers qui passent).
BRIEFS_PER_MARKET: int = _get_int("BRIEFS_PER_MARKET", 6)
#: Note mini (0-100) donnée par le LLM pour garder un clip. 0 = tout garder.
CLIP_MIN_SCORE: float = _get_float("CLIP_MIN_SCORE", 55.0)
#: Le LLM peut raccourcir le clip (couper un début mou / une fin qui traîne).
SMART_TRIM: bool = _get_bool("SMART_TRIM", True)
#: Fenêtre de durée acceptable d'un clip final (secondes).
CLIP_MIN_DURATION_S: int = _get_int("CLIP_MIN_DURATION_S", 8)
CLIP_MAX_DURATION_S: int = _get_int("CLIP_MAX_DURATION_S", 60)


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
#: le visage + le buste + le contexte de la webcam, pas juste la tête).
CAM_ZOOM: float = _get_float("CAM_ZOOM", 4.2)
#: Largeur mini de la zone facecam = fraction de la largeur source. Empêche un
#: cadrage trop serré (donc trop "zoomé"/pixelisé) quand le visage est petit.
CAM_MIN_WIDTH_FRAC: float = _get_float("CAM_MIN_WIDTH_FRAC", 0.45)
#: Mode "fond flou" : hauteur occupée par la vidéo (fraction de l'écran). 0.32 =
#: vidéo 16:9 entière mais minuscule ; 0.50 = on rogne un peu les bords pour une
#: vidéo ~1,5x plus grande (bien plus regardable sur TikTok).
BLUR_FG_HEIGHT_FRAC: float = _get_float("BLUR_FG_HEIGHT_FRAC", 0.50)
FFMPEG_BIN: str = _get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN: str = _get("FFPROBE_BIN", "ffprobe")
#: Encodage FINAL (le seul avec perte). crf plus bas = meilleure image, fichier plus lourd.
FINAL_CRF: int = _get_int("FINAL_CRF", 19)
FINAL_PRESET: str = _get("FINAL_PRESET", "fast")
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
SUB_FONT_SIZE: int = _get_int("SUB_FONT_SIZE", 100)  # gros = style TikTok
SUB_PRIMARY_COLOR: str = _get("SUB_PRIMARY_COLOR", "&H00FFFFFF")  # blanc (ASS BGR)
SUB_HIGHLIGHT_COLOR: str = _get("SUB_HIGHLIGHT_COLOR", "&H0000F0FF")  # jaune vif
SUB_OUTLINE_COLOR: str = _get("SUB_OUTLINE_COLOR", "&H00000000")  # noir
SUB_OUTLINE_WIDTH: int = _get_int("SUB_OUTLINE_WIDTH", 6)   # contour épais = lisible
SUB_SHADOW: int = _get_int("SUB_SHADOW", 4)
SUB_MARGIN_V: int = _get_int("SUB_MARGIN_V", 620)          # remonte le texte (zone safe TikTok)
SUB_UPPERCASE: bool = _get_bool("SUB_UPPERCASE", True)     # MAJUSCULES = punch
#: Retire la ponctuation en début/fin de chaque mot affiché (rendu plus propre).
SUB_STRIP_PUNCT: bool = _get_bool("SUB_STRIP_PUNCT", True)
SUB_MAX_WORDS_PER_LINE: int = _get_int("SUB_MAX_WORDS_PER_LINE", 3)

#: Max de caractères par groupe : au-delà, la ligne passerait sur 2 lignes.
SUB_MAX_CHARS_PER_LINE: int = _get_int("SUB_MAX_CHARS_PER_LINE", 18)

# Titre d'accroche (hook) affiché en haut du clip, toute la durée (image PNG).
TITLE_ENABLE: bool = _get_bool("TITLE_ENABLE", True)
#: Surligne en jaune les mots "punch" du titre (comme les captions virales).
TITLE_HIGHLIGHT: bool = _get_bool("TITLE_HIGHLIGHT", True)
#: Titre rendu en image (natif TikTok) au lieu du texte ASS : "box" (boîtes
#: blanches arrondies, texte noir TikTok Sans — le look TikTok) | "outline".
TITLE_STYLE: str = _get("TITLE_STYLE", "outline")
TITLE_CARD_FONT_SIZE: int = _get_int("TITLE_CARD_FONT_SIZE", 90)
#: Largeur max du texte du titre (fraction de la largeur vidéo).
TITLE_CARD_MAX_WIDTH: float = _get_float("TITLE_CARD_MAX_WIDTH", 0.84)


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
    "twitch": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
    "kick": [],  # scraping public, pas de clé
    "detection": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
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
