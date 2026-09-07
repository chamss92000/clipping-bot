# clipping-bot

Pipeline de clipping **100 % automatisé, budget 0 €** : détecte les vidéos les
plus chaudes (YouTube / Twitch / Kick), extrait les moments viraux (LLM),
recadre en 9:16 avec face-tracking, ajoute des sous-titres animés et publie sur
TikTok — sans intervention humaine. Tourne sur **GitHub Actions** (cron 6h) et
persiste son état sur **Google Drive**.

## Architecture (bloc par bloc)

| Bloc | Rôle | Stack | Statut |
|------|------|-------|--------|
| 1 | Détection tendances | YouTube Data API v3, Twitch Helix, Kick API | ✅ implémenté |
| 2 | Téléchargement + storage | yt-dlp, Google Drive API | ⏳ à venir |
| 3 | Transcription | openai-whisper (base, CPU) | ⏳ |
| 4 | Moments viraux | Gemini 1.5 Flash | ⏳ |
| 5 | Découpe + recadrage 9:16 | FFmpeg, MediaPipe | ⏳ |
| 6 | Sous-titres animés | ASS + FFmpeg | ⏳ |
| 7 | Publication TikTok | Upload-Post (+ fallback API off.) | ⏳ |
| 8 | Orchestration | GitHub Actions | ✅ squelette |

## Structure

```
clipping-bot/
├── .github/workflows/pipeline.yml   # cron 6h + dispatch manuel
├── config/settings.py               # toute la config + validation par bloc
├── src/
│   ├── models.py                    # VideoCandidate, Platform
│   ├── utils/                       # logging + retry (backoff exponentiel)
│   ├── detection/                   # BLOC 1 : youtube / twitch / kick + agrégateur
│   ├── downloader/                  # BLOC 2
│   ├── storage/                     # BLOC 2 : Drive + state.json
│   ├── transcription/               # BLOC 3
│   ├── viral/                       # BLOC 4
│   ├── editing/                     # BLOCS 5 & 6
│   └── publisher/                   # BLOC 7
└── main.py                          # orchestrateur
```

## Installation (dev local)

```bash
cd clipping-bot
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # puis renseigner les clés
```

FFmpeg est requis (binaire système) : `sudo apt install ffmpeg` / `choco install ffmpeg`.

## Tester le Bloc 1 (détection)

```bash
# Toutes les sources (respecte les clés présentes dans .env) :
python -m src.detection

# Une ou deux sources ciblées :
python -m src.detection youtube
python -m src.detection twitch kick

# Chaque module isolément :
python -m src.detection.youtube
python -m src.detection.twitch
python -m src.detection.kick

# Sortie JSON brute :
python -m src.detection --json

# Via l'orchestrateur (dump dans work/candidates.json) :
python main.py --detect-only
```

> Kick n'a pas d'API officielle et est protégé par Cloudflare : le module est
> best-effort et renvoie une liste vide sans casser le pipeline s'il est bloqué.

## Secrets GitHub Actions

À définir dans *Settings → Secrets and variables → Actions* :
`YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GEMINI_API_KEY`,
`GOOGLE_DRIVE_CREDENTIALS` (JSON du service account), `GOOGLE_DRIVE_FOLDER_ID`,
`UPLOADPOST_API_KEY`, `UPLOADPOST_USER`.

## Contraintes

Budget 0 € (free tiers + open source) · tout sur GitHub Actions (2000 min/mois) ·
état persistant sur Google Drive · code modulaire et testable bloc par bloc ·
retries + fallback + logs sur chaque bloc · Python 3.11+.
