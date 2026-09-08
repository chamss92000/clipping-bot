"""Bloc 4 — Détection des moments viraux via Gemini 1.5 Flash.

Envoie le transcript timestampé à Gemini avec un prompt structuré et exige une
sortie **JSON stricte**. Retourne 3 à 5 moments classés par potentiel viral :
`[{start, end, score, hook, hashtags, reason}]`.

Garde-fous :
  * **Rate limit** free tier (15 req/min) : espacement min entre appels.
  * **JSON mode** (`response_mime_type=application/json`) + parsing défensif.
  * **Validation/clamp** : timestamps bornés à la durée réelle, durée de chaque
    clip ramenée dans [CLIP_MIN_DURATION_S, CLIP_MAX_DURATION_S], nombre de
    moments plafonné à GEMINI_MAX_CLIPS.

Critères transmis au modèle : punchlines, tension, révélations, réactions
fortes, accroches. Hook + hashtags générés dans la langue du transcript.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from config import settings
from src.transcription.whisper_transcribe import Transcript
from src.utils.logging import get_logger
from src.utils.retry import retry

log = get_logger("viral")

# Espacement du rate limit (module-level).
_last_call_ts: float = 0.0


@dataclass
class ViralMoment:
    start: float
    end: float
    score: float           # 0..100 estimé par le LLM
    hook: str              # titre accrocheur pour le clip
    hashtags: list[str] = field(default_factory=list)
    reason: str = ""       # justification courte (debug/log)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return asdict(self)


class ViralDetectionError(RuntimeError):
    pass


_PROMPT_TEMPLATE = """Tu es un expert du clipping viral sur TikTok/Reels/Shorts.
On te donne la transcription horodatée (en secondes) d'une vidéo de {duration:.0f}s.
Identifie les {min_clips} à {max_clips} MEILLEURS moments à transformer en clips verticaux.

Critères d'un bon moment viral :
- punchline, phrase choc, réplique mémorable
- montée de tension, suspense, rebondissement
- révélation, aveu, information surprenante
- réaction émotionnelle forte (rire, colère, choc, hype)
- accroche des 3 premières secondes qui donne envie de rester

Contraintes STRICTES sur chaque clip :
- durée entre {clip_min} et {clip_max} secondes
- `start` et `end` en secondes (décimales), dans [0, {duration:.0f}]
- le clip DOIT commencer sur un DÉBUT DE PHRASE (jamais au milieu d'une phrase)
  et se terminer sur une fin de phrase — un extrait auto-suffisant, compréhensible seul
- commence légèrement avant l'accroche pour garder le contexte
- les 3 premières secondes doivent accrocher (pas de blanc / pas de mou au début)
- les moments ne doivent PAS se chevaucher

Le `score` (0-100) reflète le VRAI potentiel viral. Sois SÉVÈRE : ne mets un
score élevé que si le moment est réellement fort. Un contenu plat = score bas.

Le `hook` est un TITRE d'accroche court (max ~70 caractères) dans la LANGUE de
la vidéo, qui crée de la CURIOSITÉ (question, cliffhanger, promesse) sans
spoiler la chute. Pas de ponctuation finale superflue.

Réponds UNIQUEMENT avec un tableau JSON valide, sans texte autour, au format :
[
  {{
    "start": 12.5,
    "end": 45.0,
    "score": 87,
    "hook": "titre court qui donne envie de regarder",
    "hashtags": ["#tag1", "#tag2", "#tag3"],
    "reason": "pourquoi ce moment est viral (1 phrase)"
  }}
]

Transcription horodatée :
{transcript}
"""


def _rate_limit() -> None:
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    wait = settings.GEMINI_MIN_INTERVAL_S - elapsed
    if wait > 0:
        log.debug("Gemini: rate limit, pause %.1fs", wait)
        time.sleep(wait)
    _last_call_ts = time.time()


def _format_transcript(transcript: Transcript) -> str:
    """Une ligne par segment : `[start-end] texte`. Compact pour le contexte."""
    lines = []
    for s in transcript.segments:
        text = s.text.strip()
        if text:
            lines.append(f"[{s.start:.1f}-{s.end:.1f}] {text}")
    return "\n".join(lines)


def _extract_json_array(raw: str) -> list:
    """Parse défensif : tente json.loads, sinon extrait le premier tableau [...]"""
    raw = raw.strip()
    # Retire d'éventuels fences ```json ... ```
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, flags=re.DOTALL)
        if not match:
            raise ViralDetectionError(f"Réponse Gemini non-JSON : {raw[:200]}")
        data = json.loads(match.group(0))
    if isinstance(data, dict):  # tolère un objet unique
        data = [data]
    if not isinstance(data, list):
        raise ViralDetectionError("La réponse JSON n'est pas un tableau")
    return data


# base_delay élevé : sur un 429 (quota/min épuisé), il faut attendre ~30-60s.
@retry(exceptions=(Exception,), attempts=5, base_delay=20.0, max_delay=65.0)
def _call_gemini(prompt: str) -> str:
    import google.generativeai as genai

    if not settings.GEMINI_API_KEY:
        raise settings.ConfigError("GEMINI_API_KEY manquant")
    genai.configure(api_key=settings.GEMINI_API_KEY)
    model = genai.GenerativeModel(settings.GEMINI_MODEL)

    _rate_limit()
    resp = model.generate_content(
        prompt,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    )
    if not getattr(resp, "text", None):
        raise ViralDetectionError("Réponse Gemini vide (peut-être bloquée par les filtres)")
    return resp.text


def _sanitize(raw_moments: list, duration: float) -> list[ViralMoment]:
    moments: list[ViralMoment] = []
    for m in raw_moments:
        try:
            start = max(0.0, float(m["start"]))
            end = float(m["end"])
        except (KeyError, TypeError, ValueError):
            log.warning("Gemini: moment ignoré (start/end invalide) : %s", m)
            continue

        end = min(end, duration) if duration else end
        if end <= start:
            continue

        # Clamp de durée dans [CLIP_MIN, CLIP_MAX].
        dur = end - start
        if dur < settings.CLIP_MIN_DURATION_S:
            end = min(start + settings.CLIP_MIN_DURATION_S, duration or (start + settings.CLIP_MIN_DURATION_S))
        elif dur > settings.CLIP_MAX_DURATION_S:
            end = start + settings.CLIP_MAX_DURATION_S
        if end - start < 1.0:
            continue

        hashtags = m.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [h.strip() for h in hashtags.split() if h.strip()]
        hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags][:8]

        try:
            score = float(m.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0

        moments.append(
            ViralMoment(
                start=round(start, 2),
                end=round(end, 2),
                score=round(score, 2),
                hook=str(m.get("hook", "")).strip()[:150],
                hashtags=hashtags,
                reason=str(m.get("reason", "")).strip()[:300],
            )
        )

    # Seuil de qualité : on écarte les moments faibles (qualité > quantité).
    before = len(moments)
    moments = [m for m in moments if m.score >= settings.GEMINI_MIN_SCORE]
    if before and not moments:
        log.info("Viral: tous les moments sous le seuil de qualité (%.0f) — aucun clip.", settings.GEMINI_MIN_SCORE)

    # Tri par score décroissant, plafonné.
    moments.sort(key=lambda x: x.score, reverse=True)
    return moments[: settings.GEMINI_MAX_CLIPS]


def detect_moments(transcript: Transcript, video_duration_s: float) -> list[ViralMoment]:
    """Retourne les meilleurs moments viraux du transcript (liste possiblement vide)."""
    if not transcript.segments:
        log.warning("Viral: transcript vide, aucun moment.")
        return []

    prompt = _PROMPT_TEMPLATE.format(
        duration=video_duration_s or transcript.duration,
        min_clips=settings.GEMINI_MIN_CLIPS,
        max_clips=settings.GEMINI_MAX_CLIPS,
        clip_min=settings.CLIP_MIN_DURATION_S,
        clip_max=settings.CLIP_MAX_DURATION_S,
        transcript=_format_transcript(transcript),
    )

    log.info("Viral: appel Gemini (%s) sur %d segments…", settings.GEMINI_MODEL, len(transcript.segments))
    raw = _call_gemini(prompt)
    moments = _sanitize(_extract_json_array(raw), video_duration_s or transcript.duration)
    log.info("Viral: %d moments retenus", len(moments))
    for m in moments:
        log.info("  [%.1f-%.1f] score=%.0f | %s", m.start, m.end, m.score, m.hook)
    return moments


if __name__ == "__main__":  # python -m src.viral.gemini_detect <transcript.json> [duration]
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m src.viral.gemini_detect <transcript.json> [duration_s]")
        raise SystemExit(2)

    settings.validate(["viral"])
    tr = Transcript.from_dict(json.loads(open(sys.argv[1], encoding="utf-8").read()))
    dur = float(sys.argv[2]) if len(sys.argv) >= 3 else tr.duration
    for m in detect_moments(tr, dur):
        print(f"\n[{m.start:.1f}-{m.end:.1f}] ({m.duration:.0f}s) score={m.score:.0f}")
        print(f"  HOOK: {m.hook}")
        print(f"  TAGS: {' '.join(m.hashtags)}")
        print(f"  WHY : {m.reason}")
