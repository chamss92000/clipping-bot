"""Bloc 7 — Publication TikTok via Upload-Post.

API Upload-Post (plan free = 10 vidéos/mois) :
  POST https://api.upload-post.com/api/upload
  Header  : Authorization: Apikey <clé>
  Champs  : video=@fichier, title=<caption>, user=<profil>, platform[]=tiktok

Fonctions :
  * `build_caption(hook, hashtags)` : caption + hashtags (tronqué proprement).
  * `next_publish_slot(now)` : prochaine heure de pic (PUBLISH_HOURS, TIMEZONE).
  * `publish(clip, caption, hashtags, schedule_at=None)` : publie (ou simule
    si settings.DRY_RUN) et renvoie un PublishResult.

Sécurité / quota :
  * `DRY_RUN` (défaut recommandé en test) : aucune requête réseau, on log ce
    qui *serait* envoyé et on renvoie status="dry_run" — ne consomme pas le quota.
  * Le suivi du quota mensuel (10/mois) est porté par `State.uploads_left` /
    `record_upload` ; l'orchestrateur vérifie AVANT d'appeler publish().

La planification (`scheduled_date`) est best-effort : si Upload-Post ne la gère
pas sur ton plan, la vidéo part immédiatement — le pipeline reste fonctionnel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from config import settings
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("publisher.tiktok")

_UPLOAD_ENDPOINT = "/api/upload"
_MAX_CAPTION = 2200  # limite confortable pour TikTok


@dataclass
class PublishResult:
    platform: str
    status: str                       # "posted" | "scheduled" | "dry_run" | "failed"
    post_id: str | None = None
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
    """Fuseau TIMEZONE, avec fallback UTC si la base tz est absente (Windows sans tzdata)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.TIMEZONE)
    except Exception as exc:  # noqa: BLE001
        log.warning("Fuseau %s indisponible (%s), fallback UTC", settings.TIMEZONE, exc)
        return timezone.utc


def next_publish_slot(now: datetime | None = None) -> datetime:
    """Prochaine heure de pic (parmi PUBLISH_HOURS), dans le fuseau configuré."""
    tz = _tzinfo()
    now = (now or datetime.now(tz)).astimezone(tz)
    hours = sorted(set(settings.PUBLISH_HOURS)) or [18]
    for h in hours:
        cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > now:
            return cand
    # Toutes les heures d'aujourd'hui sont passées => première heure demain.
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=hours[0], minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------
@retry(exceptions=(requests.RequestException,), attempts=3)
def _post_upload(clip_path: Path, title: str, scheduled_at: datetime | None) -> dict:
    url = settings.UPLOADPOST_BASE_URL.rstrip("/") + _UPLOAD_ENDPOINT
    headers = {"Authorization": f"Apikey {settings.UPLOADPOST_API_KEY}"}
    data = [("title", title), ("user", settings.UPLOADPOST_USER or "")]
    for platform in settings.PLATFORM_TARGETS:
        data.append(("platform[]", platform))
    if scheduled_at is not None:
        # best-effort : ISO8601 UTC
        data.append(("scheduled_date", scheduled_at.astimezone(timezone.utc).isoformat()))

    with open(clip_path, "rb") as fh:
        files = {"video": (clip_path.name, fh, "video/mp4")}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=180)

    # 4xx = erreur définitive (clé/quota/format) : on ne retry pas.
    if 400 <= resp.status_code < 500:
        raise PublishError(f"Upload-Post {resp.status_code} : {resp.text[:300]}")
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"success": True, "raw_text": resp.text}


def publish(
    clip_path: str | Path,
    caption: str,
    hashtags: list[str],
    schedule_at: datetime | None = None,
) -> PublishResult:
    """Publie un clip sur les plateformes cibles (TikTok par défaut).

    Respecte settings.DRY_RUN (aucun appel réseau, ne consomme pas le quota).
    """
    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise PublishError(f"Clip introuvable : {clip_path}")

    title = build_caption(caption, hashtags)
    platform = settings.PLATFORM_TARGETS[0] if settings.PLATFORM_TARGETS else "tiktok"

    if settings.DRY_RUN:
        log.info(
            "[DRY_RUN] Publication simulée sur %s | fichier=%s | schedule=%s\n  caption: %s",
            settings.PLATFORM_TARGETS,
            clip_path.name,
            schedule_at.isoformat() if schedule_at else "immédiat",
            title.replace("\n", " ⏎ "),
        )
        return PublishResult(platform=platform, status="dry_run", scheduled_at=schedule_at)

    if not (settings.UPLOADPOST_API_KEY and settings.UPLOADPOST_USER):
        raise settings.ConfigError("UPLOADPOST_API_KEY / UPLOADPOST_USER manquant")

    log.info("Publication %s sur %s (schedule=%s)…", clip_path.name, settings.PLATFORM_TARGETS,
             schedule_at.isoformat() if schedule_at else "immédiat")
    payload = _post_upload(clip_path, title, schedule_at)

    success = bool(payload.get("success", False))
    result = PublishResult(
        platform=platform,
        status=("scheduled" if schedule_at else "posted") if success else "failed",
        post_id=payload.get("request_id"),
        scheduled_at=schedule_at,
        error=None if success else payload.get("message", "échec inconnu"),
        raw=payload,
    )
    log.info("Publication → status=%s request_id=%s", result.status, result.post_id)
    return result


if __name__ == "__main__":  # python -m src.publisher.tiktok <clip.mp4> [caption]
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m src.publisher.tiktok <clip.mp4> [caption]")
        raise SystemExit(2)
    clip = sys.argv[1]
    cap = sys.argv[2] if len(sys.argv) >= 3 else "Ce moment est incroyable 😱"
    tags = ["#gaming", "#viral", "#fyp"]
    slot = next_publish_slot()
    print("Prochain créneau de pic :", slot.isoformat())
    res = publish(clip, cap, tags, schedule_at=slot)
    print(res)
