"""Bloc 6 — Sous-titres animés style TikTok (mot par mot surligné).

À partir des timestamps mot-à-mot de Whisper :
  * `build_ass(words, out_ass, clip_offset)` génère un fichier ASS où les mots
    apparaissent par petits groupes (SUB_MAX_WORDS_PER_LINE), le mot en cours
    d'énonciation étant surligné (couleur highlight) — l'effet "karaoké" viral.
  * `burn_subtitles(clip, ass, out)` incruste l'ASS via le filtre FFmpeg
    `subtitles`.

Détails :
  * `clip_offset` = temps de départ du clip dans le référentiel des `words`
    (= moment.start). On soustrait cet offset et on ne garde que les mots
    tombant dans la durée du clip.
  * Sur Windows, le filtre `subtitles` casse sur le ':' des chemins → on lance
    FFmpeg avec cwd = dossier de l'ASS et un nom de fichier relatif.
  * Résolution ASS calée sur OUTPUT_WIDTH x OUTPUT_HEIGHT (1080x1920).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from config import settings
from src.transcription.whisper_transcribe import Word
from src.utils.logging import get_logger

log = get_logger("editing.subtitles")


class SubtitleError(RuntimeError):
    pass


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:  # arrondi
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_header() -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {settings.OUTPUT_WIDTH}
PlayResY: {settings.OUTPUT_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{settings.SUB_FONT},{settings.SUB_FONT_SIZE},{settings.SUB_PRIMARY_COLOR},&H000000FF,{settings.SUB_OUTLINE_COLOR},&H80000000,-1,0,0,0,100,100,0,0,1,{settings.SUB_OUTLINE_WIDTH},{settings.SUB_SHADOW},2,60,60,{settings.SUB_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""


def _chunk_words(words: list[Word], n: int) -> list[list[Word]]:
    return [words[i : i + n] for i in range(0, len(words), n)]


def _clean(word: str) -> str:
    # Échappe les accolades ASS et nettoie les espaces.
    token = word.strip().replace("{", "(").replace("}", ")")
    if settings.SUB_UPPERCASE:
        token = token.upper()
    return token


def build_ass(words: list[Word], out_ass: str | Path, clip_offset: float = 0.0) -> Path:
    """Génère un fichier ASS mot-par-mot surligné pour un clip démarrant à clip_offset."""
    out_ass = Path(out_ass)
    out_ass.parent.mkdir(parents=True, exist_ok=True)

    # Recale sur le clip et filtre les mots hors clip.
    rel: list[Word] = []
    for w in words:
        s = w.start - clip_offset
        e = w.end - clip_offset
        if e <= 0:
            continue
        rel.append(Word(start=max(0.0, s), end=max(0.05, e), word=w.word))

    hl = settings.SUB_HIGHLIGHT_COLOR
    primary = settings.SUB_PRIMARY_COLOR
    events: list[str] = []

    for chunk in _chunk_words(rel, settings.SUB_MAX_WORDS_PER_LINE):
        if not chunk:
            continue
        for i, w in enumerate(chunk):
            start = w.start
            # Prolonge jusqu'au mot suivant du groupe pour éviter le clignotement.
            end = chunk[i + 1].start if i + 1 < len(chunk) else w.end + 0.15
            if end <= start:
                end = start + 0.1
            # Construit la ligne : mot actif surligné, les autres en primaire.
            parts = []
            for j, wj in enumerate(chunk):
                token = _clean(wj.word)
                if j == i:
                    # Mot actif : couleur highlight + léger "pop" (scale 112%).
                    parts.append(f"{{\\c{hl}\\fscx112\\fscy112}}{token}{{\\c{primary}\\fscx100\\fscy100}}")
                else:
                    parts.append(token)
            text = " ".join(parts)
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
            )

    out_ass.write_text(_ass_header() + "\n".join(events) + "\n", encoding="utf-8")
    log.info("ASS: %d événements écrits -> %s", len(events), out_ass.name)
    return out_ass


def burn_subtitles(clip_path: str | Path, ass_path: str | Path, out_path: str | Path) -> Path:
    """Incruste l'ASS dans le clip via FFmpeg (filtre subtitles)."""
    clip_path = Path(clip_path).resolve()
    ass_path = Path(ass_path).resolve()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path = out_path.resolve()

    # cwd = dossier temp + nom relatif : contourne le ':' du chemin Windows.
    with tempfile.TemporaryDirectory() as td:
        local_ass = Path(td) / "subs.ass"
        shutil.copyfile(ass_path, local_ass)
        cmd = [
            settings.FFMPEG_BIN, "-y", "-i", str(clip_path),
            "-vf", "subtitles=subs.ass",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=td)
        if proc.returncode != 0:
            raise SubtitleError(f"FFmpeg (subtitles) a échoué : {(proc.stderr or '')[-800:]}")
    log.info("Sous-titres incrustés -> %s", out_path.name)
    return out_path


if __name__ == "__main__":  # python -m src.editing.subtitles <clip> <transcript.json> <offset> [out]
    import json
    import sys

    from src.transcription.whisper_transcribe import Transcript

    if len(sys.argv) < 4:
        print("usage: python -m src.editing.subtitles <clip.mp4> <transcript.json> <clip_offset> [out.mp4]")
        raise SystemExit(2)

    clip = sys.argv[1]
    tr = Transcript.from_dict(json.loads(open(sys.argv[2], encoding="utf-8").read()))
    offset = float(sys.argv[3])
    out = sys.argv[4] if len(sys.argv) >= 5 else str(settings.CLIPS_DIR / "clip_subbed.mp4")

    words = [w for s in tr.segments for w in s.words]
    ass = build_ass(words, settings.SUBS_DIR / "test.ass", clip_offset=offset)
    print(burn_subtitles(clip, ass, out))
