"""Bloc 7 — Publication TikTok via l'API officielle Content Posting (gratuite).

Upload-Post réserve TikTok aux plans payants → on utilise l'API officielle
TikTok, qui est gratuite. Flux "Direct Post" (FILE_UPLOAD) :

  1. Rafraîchir l'access token à partir du refresh token (OAuth).
  2. POST /v2/post/publish/video/init/  → publish_id + upload_url.
  3. PUT du fichier vidéo (chunké si > 64 Mo) sur upload_url.
  4. (Optionnel) polling du statut de publication.

Confidentialité : avant audit de l'app TikTok, seul `SELF_ONLY` (privé) est
autorisé ; après audit, passer `TIKTOK_PRIVACY_LEVEL=PUBLIC_TO_EVERYONE`.

`DRY_RUN` court-circuite tout appel réseau (aucune publication).
Le refresh token initial s'obtient via `python -m src.publisher.authorize_tiktok`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from config import settings
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("publisher.tiktok")

_MAX_CAPTION = 2200
_SINGLE_CHUNK_MAX = 64 * 1024 * 1024   # 64 Mo : au-delà, upload multi-chunk
_CHUNK = 10 * 1024 * 1024              # 10 Mo par chunk en mode multi-chunk
_MIN_CHUNK = 5 * 1024 * 1024           # contrainte TikTok : chunk >= 5 Mo


@dataclass
class PublishResult:
    platform: str
    status: str                       # "posted" | "dry_run" | "failed"
    post_id: str | None = None        # publish_id TikTok
    scheduled_at: datetime | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


class PublishError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Caption + planification
# ---------------------------------------------------------------------------
def build_caption(hook: str, hashtags: list[str]) -> str:
    tags = " ".join(h if h.startswith("#") else f"#{h}" for h in hashtags)
    caption = f"{hook.strip()}\n\n{tags}".strip() if tags else hook.strip()
    return caption[:_MAX_CAPTION]


def _tzinfo():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.TIMEZONE)
    except Exception as exc:  # noqa: BLE001
        log.warning("Fuseau %s indisponible (%s), fallback UTC", settings.TIMEZONE, exc)
        return timezone.utc


def next_publish_slot(now: datetime | None = None) -> datetime:
    """Prochaine heure de pic (parmi PUBLISH_HOURS), dans le fuseau configuré.

    NB : l'API TikTok Content Posting publie immédiatement (pas de planification
    native). On expose ce créneau pour info/logs ; la planification réelle est
    portée par la cadence du cron GitHub Actions.
    """
    tz = _tzinfo()
    now = (now or datetime.now(tz)).astimezone(tz)
    hours = sorted(set(settings.PUBLISH_HOURS)) or [18]
    for h in hours:
        cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > now:
            return cand
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=hours[0], minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# OAuth : refresh token -> access token
# ---------------------------------------------------------------------------
@retry(exceptions=(requests.RequestException,), attempts=3)
def _refresh_access_token() -> str:
    if not (settings.TIKTOK_CLIENT_KEY and settings.TIKTOK_CLIENT_SECRET and settings.TIKTOK_REFRESH_TOKEN):
        raise settings.ConfigError(
            "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN manquant "
            "(lance `python -m src.publisher.authorize_tiktok`)"
        )
    resp = requests.post(
        f"{settings.TIKTOK_API_BASE}/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": settings.TIKTOK_CLIENT_KEY,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": settings.TIKTOK_REFRESH_TOKEN,
        },
        timeout=settings.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise PublishError(f"Refresh token TikTok refusé : {data}")
    log.debug("TikTok: access token rafraîchi (expire dans %ss)", data.get("expires_in"))
    return data["access_token"]


# ---------------------------------------------------------------------------
# Direct Post : init + upload
# ---------------------------------------------------------------------------
def _plan_chunks(size: int) -> tuple[int, int]:
    """Retourne (chunk_size, total_chunk_count) conforme aux règles TikTok."""
    if size <= _SINGLE_CHUNK_MAX:
        return size, 1
    chunk = _CHUNK
    total = size // chunk  # le dernier chunk absorbe le reste (>= chunk, < 2*chunk)
    return chunk, max(1, total)


@retry(exceptions=(requests.RequestException,), attempts=3)
def _init_upload(access_token: str, title: str, size: int) -> dict:
    chunk_size, total = _plan_chunks(size)
    body = {
        "post_info": {
            "title": title,
            "privacy_level": settings.TIKTOK_PRIVACY_LEVEL,
            "disable_comment": settings.TIKTOK_DISABLE_COMMENT,
            "disable_duet": settings.TIKTOK_DISABLE_DUET,
            "disable_stitch": settings.TIKTOK_DISABLE_STITCH,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total,
        },
    }
    resp = requests.post(
        f"{settings.TIKTOK_API_BASE}/v2/post/publish/video/init/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=body,
        timeout=settings.HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise PublishError(f"init TikTok {resp.status_code} : {resp.text[:400]}")
    data = resp.json().get("data", {})
    if not data.get("upload_url") or not data.get("publish_id"):
        raise PublishError(f"Réponse init TikTok invalide : {resp.text[:400]}")
    return {"publish_id": data["publish_id"], "upload_url": data["upload_url"], "chunk_size": chunk_size, "total": total}


def _upload_file(upload_url: str, path: Path, chunk_size: int, total: int) -> None:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        for i in range(total):
            start = i * chunk_size
            end = size - 1 if i == total - 1 else start + chunk_size - 1
            fh.seek(start)
            data = fh.read(end - start + 1)
            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }
            r = requests.put(upload_url, headers=headers, data=data, timeout=180)
            if r.status_code not in (200, 201, 206):
                raise PublishError(f"upload chunk {i+1}/{total} échoué {r.status_code} : {r.text[:200]}")
            log.debug("TikTok: chunk %d/%d envoyé (%d octets)", i + 1, total, len(data))


def publish(
    clip_path: str | Path,
    caption: str,
    hashtags: list[str],
    schedule_at: datetime | None = None,
) -> PublishResult:
    """Publie un clip sur TikTok via l'API officielle. Respecte DRY_RUN."""
    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise PublishError(f"Clip introuvable : {clip_path}")

    title = build_caption(caption, hashtags)

    if settings.DRY_RUN:
        size = clip_path.stat().st_size
        chunk_size, total = _plan_chunks(size)
        log.info(
            "[DRY_RUN] Post TikTok simulé | %s (%.1f Mo, %d chunk(s)) | privacy=%s\n  caption: %s",
            clip_path.name, size / 1e6, total, settings.TIKTOK_PRIVACY_LEVEL,
            title.replace("\n", " ⏎ "),
        )
        return PublishResult(platform="tiktok", status="dry_run", scheduled_at=schedule_at)

    try:
        access = _refresh_access_token()
        size = clip_path.stat().st_size
        init = _init_upload(access, title, size)
        _upload_file(init["upload_url"], clip_path, init["chunk_size"], init["total"])
    except (PublishError, requests.RequestException, settings.ConfigError) as exc:
        log.error("Publication TikTok échouée : %s", exc)
        return PublishResult(platform="tiktok", status="failed", error=str(exc))

    log.info(
        "TikTok: publication lancée (publish_id=%s, privacy=%s)",
        init["publish_id"], settings.TIKTOK_PRIVACY_LEVEL,
    )
    return PublishResult(
        platform="tiktok",
        status="posted",
        post_id=init["publish_id"],
        scheduled_at=schedule_at,
        raw=init,
    )


if __name__ == "__main__":  # python -m src.publisher.tiktok <clip.mp4> [caption]
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m src.publisher.tiktok <clip.mp4> [caption]")
        raise SystemExit(2)
    clip = sys.argv[1]
    cap = sys.argv[2] if len(sys.argv) >= 3 else "Ce moment est incroyable 😱"
    print("Prochain créneau de pic :", next_publish_slot().isoformat())
    print(publish(clip, cap, ["#gaming", "#viral", "#fyp"]))
