# clipping-bot — clips gaming, focus GTA 6

Pipeline de clipping **100 % automatisé, budget 0 €**, centré sur **un seul type
de contenu** : les clips gaming déjà viraux (Twitch / Kick), en priorité sur
**GTA 6**. Il récupère les meilleurs moments du jour, les fait juger par une IA
qui **regarde** le clip, les remonte en 9:16 avec sous-titres animés et titre
d'accroche, puis les dépose sur Google Drive (ou publie sur TikTok).
Tourne sur **GitHub Actions** (2 runs/jour) et persiste son état sur Drive.

## Pipeline

| Étape | Rôle | Stack |
|------|------|-------|
| 1. Détection | Clips les plus vus **par jeu** (API Clips Twitch `game_id`) + chaînes Kick filtrées par jeu. Anti-doublon de moment, max N clips/streamer | Twitch Helix, Kick (best-effort) |
| 2. Téléchargement | Clip entier ≤ 1080p | yt-dlp |
| 3. Brief IA | Gemini **regarde** le clip (vidéo 360p + son) : note 0-100, recadrage (coupe le début mou), 3 titres de formules différentes, légende SEO, hashtags | Gemini Flash (gratuit), chaîne de repli |
| 4. Filtre | Note < `CLIP_MIN_SCORE` => clip écarté (avant la transcription : 0 temps perdu) | — |
| 5. Sous-titres | Transcription mot à mot, VAD anti-hallucination, vocabulaire GTA | faster-whisper `large-v3-turbo` (local) |
| 6. Montage | 9:16 auto (facecam+gameplay / visage / fond flou), sous-titres karaoké, titre animé | FFmpeg, MediaPipe, Pillow |
| 7. Livraison | Drive (dossier FR / intl) + rapport + e-mail, ou TikTok API / YouTube Shorts | Drive API |

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
DRY_RUN=true python main.py             # cycle complet sans publier
```

## Secrets GitHub Actions

`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GEMINI_API_KEY` (gratuit sur
aistudio.google.com), `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_REFRESH_TOKEN` ; optionnels : `TIKTOK_*`, `YOUTUBE_REFRESH_TOKEN`,
`SMTP_USER`, `SMTP_PASSWORD`.

Auth OAuth (une fois, en local) : `python -m src.storage.authorize` (Drive),
`python -m src.publisher.authorize_tiktok`, `python -m src.publisher.authorize_youtube`.

## Modèles IA (gratuits)

* **Titres / note / légende** : `GEMINI_MODELS` = `gemini-flash-latest,gemini-flash-lite-latest`
  (essayés dans l'ordre ; repli automatique si un modèle est retiré ou à court de quota).
* **Sous-titres** : Whisper `large-v3-turbo` en local (CPU, aucun quota). `WHISPER_MODEL=medium` pour revenir en arrière.
