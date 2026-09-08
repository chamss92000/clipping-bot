"""Bloc 7bis — Autorisation OAuth YouTube (à lancer UNE fois en local).

Récupère un `refresh_token` pour le scope `youtube.upload`, permettant l'upload
headless de Shorts (local + CI). Réutilise le même client OAuth "Desktop" que
Drive (oauth_client.json).

Usage :
    python -m src.publisher.authorize_youtube

Écrit `youtube_token.json` (utilisé en local) et affiche `YOUTUBE_REFRESH_TOKEN`
à mettre en .env / secret GitHub.

Remarque : `youtube.upload` est un scope sensible. Sur une app non vérifiée,
l'écran de consentement affichera un avertissement "application non validée" —
en tant que propriétaire de l'app, clique "Paramètres avancés" → "Accéder à…".
"""

from __future__ import annotations

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from config import settings

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    client_file = settings.GOOGLE_OAUTH_CLIENT_FILE or "./oauth_client.json"
    if not Path(client_file).exists():
        print(f"[erreur] Fichier client OAuth introuvable : {client_file}")
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(client_file, _SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    token_file = settings.YOUTUBE_OAUTH_TOKEN_FILE or "./youtube_token.json"
    Path(token_file).write_text(creds.to_json(), encoding="utf-8")

    print("\n✅ Autorisation YouTube réussie.")
    print(f"   {token_file} écrit (utilisé en local automatiquement)")
    print("\n--- Secret GitHub Actions (mode headless CI) ---")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token or '(absent — relance avec prompt=consent)'}")
    print("\n(GOOGLE_OAUTH_CLIENT_ID / CLIENT_SECRET sont déjà tes secrets Drive — réutilisés.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
