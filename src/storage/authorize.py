"""Bloc 2 — Autorisation OAuth Google Drive (à lancer UNE SEULE FOIS en local).

Ouvre le navigateur pour que TU autorises l'app à écrire dans ton Drive, puis :
  * écrit `token.json` en local (utilisé automatiquement par DriveStorage) ;
  * affiche les 3 valeurs à mettre en secrets GitHub Actions pour le mode
    headless (CI) : GOOGLE_OAUTH_CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN.

Prérequis : un fichier client OAuth "Desktop app" téléchargé depuis Google Cloud
Console (APIs & Services → Credentials → Create Credentials → OAuth client ID →
Desktop app), enregistré sous `oauth_client.json` à la racine du projet
(ou pointé par GOOGLE_OAUTH_CLIENT_FILE).

Usage :
    python -m src.storage.authorize
"""

from __future__ import annotations

import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from config import settings

# Doit correspondre exactement au scope de drive.py (drive.file, non-sensible).
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> int:
    client_file = settings.GOOGLE_OAUTH_CLIENT_FILE or "./oauth_client.json"
    if not Path(client_file).exists():
        print(
            f"[erreur] Fichier client OAuth introuvable : {client_file}\n"
            "  -> Google Cloud Console → APIs & Services → Credentials →\n"
            "     Create Credentials → OAuth client ID → Type: Desktop app →\n"
            "     télécharge le JSON et enregistre-le sous 'oauth_client.json'\n"
            "     à la racine du projet (ou définis GOOGLE_OAUTH_CLIENT_FILE)."
        )
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(client_file, _SCOPES)
    # Ouvre le navigateur, lance un petit serveur local pour capter le callback.
    creds = flow.run_local_server(port=0, prompt="consent")

    token_file = settings.GOOGLE_OAUTH_TOKEN_FILE or "./token.json"
    Path(token_file).write_text(creds.to_json(), encoding="utf-8")

    client_cfg = json.loads(Path(client_file).read_text(encoding="utf-8"))
    cfg = client_cfg.get("installed") or client_cfg.get("web") or {}

    print("\n✅ Autorisation réussie.")
    print(f"   token.json écrit dans : {token_file}  (utilisé en local automatiquement)")
    print("\n--- Secrets GitHub Actions (mode headless CI) ---")
    print(f"GOOGLE_OAUTH_CLIENT_ID={cfg.get('client_id', '')}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={cfg.get('client_secret', '')}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token or '(absent — relance avec prompt=consent)'}")
    print("\n(Ne committe JAMAIS ces valeurs ni token.json — déjà couverts par .gitignore.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
