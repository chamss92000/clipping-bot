"""Bloc 3 — Transcription Whisper (openai-whisper, modèle "base", CPU).

Produit un transcript **timestampé au mot** (`word_timestamps=True`) — c'est ce
qui permettra les sous-titres animés mot-par-mot du Bloc 6, et un découpage
précis des moments viraux au Bloc 4.

Le modèle est chargé une seule fois et mis en cache (le chargement CPU coûte
plusieurs secondes). Whisper appelle ffmpeg en interne pour extraire l'audio,
donc on peut lui passer directement un .mp4.

Contrat de sortie : `Transcript` sérialisable JSON (to_dict / from_dict) pour
persistance sur Drive et transmission au Bloc 4.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import settings
from src.utils.logging import get_logger

log = get_logger("transcription")

# Cache module-level : {nom_modèle: modèle chargé}
_MODEL_CACHE: dict[str, Any] = {}


@dataclass
class Word:
    start: float
    end: float
    word: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    segments: list[Segment] = field(default_factory=list)
    #: Décalage à ajouter aux timestamps pour revenir au référentiel de la VOD
    #: source quand on a transcrit une fenêtre échantillonnée (start de la fenêtre).
    source_offset: float = 0.0

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "source_offset": self.source_offset,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        segments = [
            Segment(
                start=s["start"],
                end=s["end"],
                text=s["text"],
                words=[Word(**w) for w in s.get("words", [])],
            )
            for s in data.get("segments", [])
        ]
        return cls(
            language=data.get("language", ""),
            segments=segments,
            source_offset=data.get("source_offset", 0.0),
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class TranscriptionError(RuntimeError):
    pass


def _load_model(name: str):
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    try:
        from faster_whisper import WhisperModel  # import tardif
    except ImportError as exc:  # pragma: no cover
        raise TranscriptionError("faster-whisper non installé (`pip install faster-whisper`).") from exc
    log.info(
        "Whisper: chargement du modèle '%s' (%s / %s)…",
        name, settings.WHISPER_DEVICE, settings.WHISPER_COMPUTE_TYPE,
    )
    model = WhisperModel(name, device=settings.WHISPER_DEVICE, compute_type=settings.WHISPER_COMPUTE_TYPE)
    _MODEL_CACHE[name] = model
    return model


def transcribe(
    media_path: str | Path,
    language: str | None = None,
    source_offset: float = 0.0,
    model_name: str | None = None,
) -> Transcript:
    """Transcrit un média et retourne un `Transcript` timestampé au mot.

    :param source_offset: ajouté à tous les timestamps (référentiel VOD source
        quand on a téléchargé une fenêtre échantillonnée).
    """
    media_path = Path(media_path)
    if not media_path.exists():
        raise TranscriptionError(f"Média introuvable : {media_path}")

    model_name = model_name or settings.WHISPER_MODEL
    language = language if language is not None else settings.WHISPER_LANGUAGE
    model = _load_model(model_name)

    log.info("Whisper: transcription de %s (lang=%s)…", media_path.name, language or "auto")
    try:
        seg_iter, info = model.transcribe(
            str(media_path),
            language=language,
            word_timestamps=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise TranscriptionError(f"Échec transcription {media_path.name} : {exc}") from exc

    segments: list[Segment] = []
    for seg in seg_iter:  # générateur : l'itération déclenche le calcul
        words = [
            Word(
                start=round(float(w.start) + source_offset, 3),
                end=round(float(w.end) + source_offset, 3),
                word=w.word,
            )
            for w in (seg.words or [])
            if w.start is not None and w.end is not None
        ]
        segments.append(
            Segment(
                start=round(float(seg.start) + source_offset, 3),
                end=round(float(seg.end) + source_offset, 3),
                text=(seg.text or "").strip(),
                words=words,
            )
        )

    transcript = Transcript(
        language=getattr(info, "language", language or ""),
        segments=segments,
        source_offset=source_offset,
    )
    log.info(
        "Whisper: %d segments, %d mots, langue=%s",
        len(segments),
        sum(len(s.words) for s in segments),
        transcript.language,
    )
    return transcript


if __name__ == "__main__":  # python -m src.transcription.whisper_transcribe <media> [lang]
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m src.transcription.whisper_transcribe <media_path> [lang]")
        raise SystemExit(2)

    media = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) >= 3 else None
    tr = transcribe(media, language=lang)
    out = settings.WORK_DIR / (Path(media).stem + ".transcript.json")
    tr.save(out)
    print(f"\nLangue: {tr.language} | {len(tr.segments)} segments | durée ~{tr.duration:.0f}s")
    print(f"Transcript JSON: {out}\n")
    for s in tr.segments[:8]:
        print(f"[{s.start:6.2f}-{s.end:6.2f}] {s.text}")
