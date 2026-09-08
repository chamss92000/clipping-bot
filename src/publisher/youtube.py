"""Bloc 7bis — Publication automatique en YouTube Shorts (API Data v3, gratuite).

Contrairement à TikTok, l'upload YouTube en **public** est immédiat, sans audit
ni domaine vérifié → c'est le canal 100% automatisable à 0€.

Auth : OAuth utilisateur (scope `youtube.upload`), même client que Drive
(GOOGLE_OAUTH_CLIENT_ID/SECRET) + un refresh token dédié obtenu via
`python -m src.publisher.authorize_youtube`.

Coût quota : `videos.insert` ≈ 1600 unités (quota/jour = 10000 ⇒ ~6 max). Le
garde-fou `State.youtube_left()` est vérifié par l'orchestrateur.

`DRY_RUN` court-circuite l'upload réseau.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import settings
from src.utils.logging import get_logger
from src.utils.retry import retry

from .tiktok import PublishResult  # dataclass réutilisée

log = get_logger("publisher.youtube")

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_TITLE_MAX = 100
_DESC_MAX = 4900


class YouTubeError(RuntimeError):
    pass


def load_credentials() -> UserCredentials:
    """OAuth youtube.upload : refresh token via env (CI) ou youtube_token.json (local)."""
    if (
        settings.GOOGLE_OAUTH_CLIENT_ID
        and settings.GOOGLE_OAUTH_CLIENT_SECRET
        and settings.YOUTUBE_REFRESH_TOKEN
    ):
        creds = UserCredentials(
            token=None,
            refresh_token=settings.YOUTUBE_REFRESH_TOKEN,
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            token_uri=_TOKEN_URI,
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        return creds

    token_file = settings.YOUTUBE_OAUTH_TOKEN_FILE
    if token_file and Path(token_file).exists():
        creds = UserCredentials.from_authorized_user_file(token_file, _SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(token_file).write_text(creds.to_json(), encoding="utf-8")
        return creds

    raise settings.ConfigError(
        "Auth YouTube absente. Lance `python -m src.publisher.authorize_youtube`."
    )


def _build_title(hook: str) -> str:
    title = " ".join(hook.strip().split()).replace("<", "").replace(">", "")
    if "#shorts" not in title.lower():
        # garde de la place pour " #Shorts"
        title = title[: _TITLE_MAX - 8].rstrip() + " #Shorts"
    return title[:_TITLE_MAX]


def _build_description(hook: str, hashtags: list[str], creator: str) -> str:
    tags = " ".join(h if h.startswith("#") else f"#{h}" for h in hashtags)
    lines = [hook.strip(), ""]
    if creator:
        lines.append(f"Source : {creator}")
    lines.append(tags)
    lines.append("#Shorts")
    return "\n".join(lines)[:_DESC_MAX]


@retry(exceptions=(HttpError, OSError), attempts=3)
def _do_upload(youtube, body: dict, clip_path: Path) -> dict:
    media = MediaFileUpload(str(clip_path), chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return response


def upload_short(
    clip_path: str | Path,
    hook: str,
    hashtags: list[str],
    creator: str = "",
) -> PublishResult:
    """Upload un clip en Short public sur YouTube. Respecte DRY_RUN."""
    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise YouTubeError(f"Clip introuvable : {clip_path}")

    title = _build_title(hook)
    description = _build_description(hook, hashtags, creator)
    tags = [h.lstrip("#") for h in hashtags][:15]

    if settings.DRY_RUN:
        log.info("[DRY_RUN] Short YouTube simulé | %s | titre: %s", clip_path.name, title)
        return PublishResult(platform="youtube", status="dry_run")

    try:
        creds = load_credentials()
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": settings.YOUTUBE_CATEGORY_ID,
            },
            "status": {
                "privacyStatus": settings.YOUTUBE_PRIVACY,
                "selfDeclaredMadeForKids": False,
            },
        }
        resp = _do_upload(youtube, body, clip_path)
    except (HttpError, OSError, settings.ConfigError, YouTubeError) as exc:
        log.error("Upload YouTube échoué : %s", exc)
        return PublishResult(platform="youtube", status="failed", error=str(exc))

    vid = resp.get("id")
    log.info("YouTube: Short publié → https://youtube.com/shorts/%s (%s)", vid, settings.YOUTUBE_PRIVACY)
    return PublishResult(platform="youtube", status="posted", post_id=vid, raw={"id": vid})


if __name__ == "__main__":  # python -m src.publisher.youtube <clip.mp4> [hook]
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m src.publisher.youtube <clip.mp4> [hook]")
        raise SystemExit(2)
    clip = sys.argv[1]
    hook = sys.argv[2] if len(sys.argv) >= 3 else "Ce moment est incroyable"
    print(upload_short(clip, hook, ["gaming", "viral", "fyp"]))
