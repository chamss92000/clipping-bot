"""Bloc 7 — Autorisation OAuth TikTok (à lancer UNE fois en local).

Récupère le `refresh_token` nécessaire à la publication headless (CI + local).

Prérequis (portail développeur TikTok, https://developers.tiktok.com/) :
  * une app avec le produit **Content Posting API** activé,
  * le scope **video.publish** ajouté,
  * l'URL de redirection **exactement** égale à TIKTOK_REDIRECT_URI
    (par défaut http://localhost:8888/callback),
  * TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET renseignés dans .env.

Flux : ouvre le navigateur → tu autorises → un mini-serveur local capte le
`code` (si redirect = localhost) OU tu colles l'URL de retour → échange contre
les tokens → affiche le refresh_token à mettre en .env / secret GitHub.

Usage :
    python -m src.publisher.authorize_tiktok
"""

from __future__ import annotations

import secrets as _secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from config import settings

_SCOPES = "user.info.basic,video.publish"
_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"


class _Handler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _Handler.code = (params.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Autorisation reçue, tu peux fermer cet onglet." if _Handler.code else "Aucun code reçu."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode("utf-8"))

    def log_message(self, *args):  # silence
        pass


def _capture_code_localhost() -> str | None:
    parsed = urllib.parse.urlparse(settings.TIKTOK_REDIRECT_URI)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    server = HTTPServer((host, port), _Handler)
    server.timeout = 300
    print(f"En attente du retour OAuth sur {settings.TIKTOK_REDIRECT_URI} …")
    server.handle_request()
    return _Handler.code


def _exchange_code(code: str) -> dict:
    resp = requests.post(
        f"{settings.TIKTOK_API_BASE}/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": settings.TIKTOK_CLIENT_KEY,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.TIKTOK_REDIRECT_URI,
        },
        timeout=settings.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    if not (settings.TIKTOK_CLIENT_KEY and settings.TIKTOK_CLIENT_SECRET):
        print("[erreur] TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants dans .env")
        return 2

    state = _secrets.token_urlsafe(16)
    params = {
        "client_key": settings.TIKTOK_CLIENT_KEY,
        "scope": _SCOPES,
        "response_type": "code",
        "redirect_uri": settings.TIKTOK_REDIRECT_URI,
        "state": state,
    }
    auth_url = f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("\nOuvre cette URL et autorise l'application :\n", auth_url, "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001
        pass

    code: str | None = None
    parsed = urllib.parse.urlparse(settings.TIKTOK_REDIRECT_URI)
    # Capture auto seulement si redirect = http://localhost (serveur local en clair).
    # En https (exigé par TikTok), le navigateur ne pourra pas joindre notre serveur
    # HTTP : on récupère alors le code manuellement depuis la barre d'adresse.
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        try:
            code = _capture_code_localhost()
        except OSError as exc:
            print(f"(serveur local indisponible : {exc})")
    if not code:
        print(
            "\nAprès avoir autorisé, ton navigateur sera redirigé vers une page qui\n"
            "n'affiche rien (normal). COPIE l'URL COMPLÈTE dans la barre d'adresse\n"
            "(elle contient ...?code=XXXX...) et colle-la ci-dessous.\n"
        )
        pasted = input("URL de redirection complète (ou juste le code) : ").strip()
        if "code=" in pasted:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [None])[0]
        else:
            code = pasted or None
    if not code:
        print("[erreur] Aucun code d'autorisation obtenu.")
        return 1

    tokens = _exchange_code(code)
    if "refresh_token" not in tokens:
        print("[erreur] Échange échoué :", tokens)
        return 1

    print("\n✅ Autorisation TikTok réussie.")
    print("\n--- À mettre dans .env et en secrets GitHub ---")
    print(f"TIKTOK_CLIENT_KEY={settings.TIKTOK_CLIENT_KEY}")
    print(f"TIKTOK_CLIENT_SECRET={settings.TIKTOK_CLIENT_SECRET}")
    print(f"TIKTOK_REFRESH_TOKEN={tokens['refresh_token']}")
    print(f"\n(open_id={tokens.get('open_id')} | scope={tokens.get('scope')} | "
          f"refresh_expires_in={tokens.get('refresh_expires_in')}s)")
    print("Ne committe jamais ces valeurs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
