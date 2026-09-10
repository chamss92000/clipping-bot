"""Générateur de vidéos faceless "valeur" (niche argent/IA) — 100% code, 0€.

Chaîne : script (Gemini) -> voix off IA (edge-tts, gratuite) -> sous-titres
mot-à-mot (Whisper + ASS, réutilise le bloc clips) -> b-roll libre de droits
(Pexels si clé, sinon dégradé animé) -> montage vertical 1080x1920.

100% faceless (voix synthétique, jamais la voix/le visage de l'utilisateur).
Muet ? Non : la voix off EST le contenu. L'utilisateur poste sur TikTok et met
les liens d'affiliation en bio.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from config import settings
from src.utils.logging import get_logger

log = get_logger("generate.faceless")


class FacelessError(RuntimeError):
    pass


_SCRIPT_PROMPT = """You are a top faceless short-form scriptwriter for the niche:
"{topic}".

Write ONE vertical video script that delivers concrete value FAST (spoken
narration of about {words} words). Rules:
- First sentence = a strong HOOK (curiosity or big benefit), no intro/fluff.
- Give specific, actionable points (name concrete tool CATEGORIES or steps).
- Punchy spoken English, short sentences, address the viewer as "you".
- No emojis, no hashtags inside the narration, no markdown.
- End with a soft CTA to follow for more.

Return ONLY valid JSON, no text around:
{{"hook":"<3 to 6 word on-screen title, uppercase-friendly>",
  "narration":"<the full spoken script, one flowing text>",
  "hashtags":["#tag1","#tag2","#tag3","#tag4"],
  "bg_keywords":"<2-3 words to search stock b-roll, e.g. 'laptop desk work'>"}}"""


def generate_script(topic: str | None = None, words: int | None = None) -> dict:
    """Génère un script via Gemini. Retourne {hook, narration, hashtags, bg_keywords}."""
    from src.viral.gemini_detect import _call_gemini

    topic = topic or settings.FACELESS_TOPIC
    words = words or settings.FACELESS_WORDS
    raw = _call_gemini(_SCRIPT_PROMPT.format(topic=topic, words=words)).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    data = json.loads(raw)
    narration = str(data.get("narration", "")).strip()
    if not narration:
        raise FacelessError("Script Gemini sans narration")
    hashtags = data.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = hashtags.split()
    hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags][:8]
    return {
        "hook": str(data.get("hook", "")).strip()[:80] or topic[:40],
        "narration": narration,
        "hashtags": hashtags,
        "bg_keywords": str(data.get("bg_keywords", "") or "technology background").strip(),
    }


def synth_voice(text: str, out_mp3: Path) -> Path:
    """Voix off via edge-tts (CLI). Écrit un mp3."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        txt = f.name
    try:
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", settings.FACELESS_VOICE, "--rate", settings.FACELESS_VOICE_RATE,
            "--file", txt, "--write-media", str(out_mp3),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out_mp3.exists() or out_mp3.stat().st_size < 1000:
            raise FacelessError(f"edge-tts a échoué : {(proc.stderr or '')[-400:]}")
    finally:
        Path(txt).unlink(missing_ok=True)
    return out_mp3


def _duration(path: Path) -> float:
    out = subprocess.run(
        [settings.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _gradient_bg(duration: float, out: Path) -> Path:
    """Fond dégradé animé (aucune dépendance externe)."""
    cmd = [
        settings.FFMPEG_BIN, "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"gradients=s={settings.OUTPUT_WIDTH}x{settings.OUTPUT_HEIGHT}"
              ":c0=0x0F2027:c1=0x203A43:c2=0x2C5364:x0=0:y0=0"
              f":x1={settings.OUTPUT_WIDTH}:y1={settings.OUTPUT_HEIGHT}:speed=0.012:d={duration:.2f}",
        "-t", f"{duration:.2f}", "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FacelessError(f"Fond dégradé KO : {(proc.stderr or '')[-300:]}")
    return out


def _pexels_bg(duration: float, keywords: str, out: Path) -> Path | None:
    """Télécharge un b-roll vertical libre de droits (Pexels). None si indispo."""
    if not settings.PEXELS_API_KEY:
        return None
    import requests

    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": settings.PEXELS_API_KEY},
            params={"query": keywords, "orientation": "portrait", "per_page": 12, "size": "medium"},
            timeout=settings.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        vids = r.json().get("videos", [])
        best_url = None
        best_h = 0
        for v in vids:
            for f in v.get("video_files", []):
                h = f.get("height") or 0
                if (f.get("width") or 0) < (f.get("height") or 0) and h >= 1200 and h > best_h:
                    best_h, best_url = h, f.get("link")
        if not best_url:
            return None
        raw = out.with_suffix(".src.mp4")
        with requests.get(best_url, stream=True, timeout=60) as dl:
            dl.raise_for_status()
            with open(raw, "wb") as fh:
                for chunk in dl.iter_content(1 << 20):
                    fh.write(chunk)
        # scale/crop 1080x1920, boucle jusqu'à la durée voulue, léger assombrissement
        cmd = [
            settings.FFMPEG_BIN, "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", str(raw), "-t", f"{duration:.2f}",
            "-vf", f"scale={settings.OUTPUT_WIDTH}:{settings.OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
                   f"crop={settings.OUTPUT_WIDTH}:{settings.OUTPUT_HEIGHT},eq=brightness=-0.06",
            "-an", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        raw.unlink(missing_ok=True)
        if proc.returncode != 0:
            return None
        return out
    except Exception as exc:  # noqa: BLE001 - on retombe sur le dégradé
        log.warning("Pexels indisponible (%s) — fond dégradé.", exc)
        return None


def make_faceless_video(out_path: str | Path, script: dict | None = None) -> dict:
    """Produit une vidéo faceless complète. Retourne les métadonnées (hook, hashtags…)."""
    from src.editing.subtitles import build_ass, burn_subtitles
    from src.transcription.whisper_transcribe import transcribe

    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    script = script or generate_script()

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        voice = synth_voice(script["narration"], tdp / "voice.mp3")
        dur = _duration(voice)
        if dur < 2:
            raise FacelessError("Voix off trop courte")

        bg = _pexels_bg(dur, script["bg_keywords"], tdp / "bg.mp4") or _gradient_bg(dur, tdp / "bg.mp4")

        # vidéo (bg) + audio (voix)
        raw = tdp / "raw.mp4"
        mux = subprocess.run(
            [settings.FFMPEG_BIN, "-y", "-loglevel", "error", "-i", str(bg), "-i", str(voice),
             "-map", "0:v:0", "-map", "1:a:0", "-shortest",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(raw)],
            capture_output=True, text=True,
        )
        if mux.returncode != 0:
            raise FacelessError(f"Mux bg+voix KO : {(mux.stderr or '')[-300:]}")

        # sous-titres mot-à-mot (Whisper sur la voix off) + titre = hook
        transcript = transcribe(voice)
        words = [w for s in transcript.segments for w in s.words]
        ass = build_ass(words, tdp / "subs.ass", clip_offset=0.0,
                        title=script["hook"], clip_duration=dur)
        burn_subtitles(raw, ass, out_path)

    log.info("Faceless: vidéo écrite %s (%.0fs)", out_path.name, dur)
    return {"hook": script["hook"], "hashtags": script["hashtags"],
            "narration": script["narration"], "duration": round(dur, 1)}


if __name__ == "__main__":  # python -m src.generate.faceless [out.mp4]
    import sys

    settings.validate(["viral"])
    out = sys.argv[1] if len(sys.argv) > 1 else str(settings.WORK_DIR / "faceless_test.mp4")
    meta = make_faceless_video(out)
    print(out)
    print("HOOK:", meta["hook"], "| tags:", " ".join(meta["hashtags"]))
