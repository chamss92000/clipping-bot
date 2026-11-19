# clipping-bot — clips gaming, focus GTA 6

Pipeline de clipping **100 % automatisé, budget 0 €**, centré sur **un seul type
de contenu** : les clips gaming déjà viraux de Twitch, en priorité sur **GTA 6**.
Il récupère les meilleurs moments du jour, les fait juger par une IA qui
**regarde** le clip, les remonte en 9:16 avec sous-titres animés et titre
putaclic, puis les dépose sur Google Drive avec la légende prête à copier.
Tu postes à la main sur TikTok.
Tourne sur **GitHub Actions** (2 runs/jour) et persiste son état sur Drive.

## Pipeline

| Étape | Rôle | Stack |
|------|------|-------|
| 1. Détection | Clips les plus vus **par jeu** (API Clips Twitch `game_id`), seuil de vues adaptatif, anti-doublon de moment, max N clips/streamer | Twitch Helix |
| 2. Téléchargement | Clip entier ≤ 1080p | yt-dlp |
| 3. Brief IA | Gemini **regarde** le clip (vidéo 360p + son) : note 0-100, recadrage (coupe le début mou), 3 titres putaclic de leviers différents, légende SEO, hashtags | Gemini Flash (gratuit), chaîne de repli |
| 4. Sélection | Note < `CLIP_MIN_SCORE` => écarté ; on note jusqu'à 6 clips par chaîne et on ne monte que les 3 meilleurs. Clip non noté = jamais publié | — |
| 5. Sous-titres | Transcription mot à mot, VAD anti-hallucination, vocabulaire GTA | faster-whisper `large-v3-turbo` (local) |
| 6. Montage | 9:16 auto (facecam+gameplay / visage / fond flou), sous-titres karaoké, titre animé | FFmpeg, MediaPipe, Pillow |
| 7. Livraison | Drive (dossier FR / intl) + `rapport.md` (légendes) + e-mail | Drive API, SMTP |

## Choisir les jeux

Dans *Settings → Secrets and variables → Actions → Variables* (sans toucher au code) :

| Variable | Défaut | Rôle |
|---|---|---|
| `GAMES_PRIMARY` | `Grand Theft Auto VI` | Jeux prioritaires (nom **exact** de la catégorie Twitch), bonus de score |
| `GAMES_SECONDARY` | `Grand Theft Auto V` | Complètent le quota les jours creux. `""` = prioritaires seulement |
| `CLIP_MIN_SCORE` | `55` | Note IA mini. Monte à 65-70 pour être plus exigeant |
| `TWITCH_CLIPS_DAYS` | `3` | Fraîcheur des clips |
| `TWITCH_CLIP_MIN_VIEWS` | `100` | Vues mini d'un clip |
| `MAX_CLIPS_PER_STREAMER` | `2` | Diversité |
| `CLIPS_PER_MARKET` | `3` | Clips par chaîne (FR / intl) et par run |

## Installation (dev local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # puis renseigner les clés
python -m pytest -q tests     # tests rapides, sans réseau
```

FFmpeg est requis : `sudo apt install ffmpeg` / `choco install ffmpeg`.

## Tester chaque étape

```bash
python -m src.detection                 # classement des clips détectés
python main.py --detect-only            # idem + dump work/candidates.json
python -m src.downloader.download <url_du_clip>
python -m src.viral.gemini clip.mp4 "Grand Theft Auto VI" fr   # brief IA d'un clip local
python -m src.editing.title_card "Il vole un jet dans GTA 6"
DRY_RUN=true python main.py             # cycle complet, sans e-mail
```

## Secrets GitHub Actions

`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GEMINI_API_KEY` (gratuit sur
aistudio.google.com), `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_REFRESH_TOKEN` ; pour l'e-mail : `SMTP_USER`, `SMTP_PASSWORD`
(+ variable `NOTIFY_EMAIL_TO`).

Auth Drive (une fois, en local) : `python -m src.storage.authorize`.

## Modèles IA (gratuits)

* **Titres / note / légende** : `GEMINI_MODELS` = `gemini-flash-latest,gemini-flash-lite-latest`
  (essayés dans l'ordre ; repli automatique si un modèle est retiré ou à court de quota).
* **Sous-titres** : Whisper `large-v3-turbo` en local (CPU, aucun quota). `WHISPER_MODEL=medium` pour revenir en arrière.
