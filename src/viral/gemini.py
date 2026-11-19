"""Bloc 4 — « Brief » d'un clip par un LLM multimodal gratuit (Gemini).

Le modèle REGARDE le clip (vidéo basse définition + audio) et renvoie en UN
appel :
  * une description de ce qui se passe (force le modèle à comprendre avant d'écrire) ;
  * une note de potentiel viral 0-100 (filtre qualité : on jette les clips plats) ;
  * un recadrage temporel optionnel (couper un début mou / une fin qui traîne) ;
  * 3 titres d'accroche de formules différentes + l'index du meilleur ;
  * une légende courte orientée recherche (SEO TikTok) et des hashtags.

Pourquoi la vidéo : en gaming (GTA 6), l'essentiel se passe À L'ÉCRAN (une
course-poursuite, un bug, une découverte) et souvent sans dialogue. Un LLM qui ne
lit que la transcription ne peut pas écrire un titre précis sur ce qu'il ne voit pas.

Robustesse :
  * chaîne de modèles (GEMINI_MODELS) : repli sur 404 (modèle retiré) / 429 (quota) ;
  * sortie JSON typée (schéma) + parsing défensif ;
  * validation locale des titres (1re personne, longueur, répétition) ;
  * en cas d'échec total : repli sur le titre Twitch nettoyé (le clip passe quand même).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from config import settings
from src.detection.games import game_hashtag, info_for
from src.utils.logging import get_logger

log = get_logger("viral")

_last_call_ts: float = 0.0
#: Disjoncteur : échecs COMPLETS consécutifs (toute la chaîne de modèles). Au-delà
#: de _MAX_CHAIN_FAILURES, on n'appelle plus le LLM de tout le cycle (quota du
#: jour épuisé / panne) : chaque clip perdrait sinon 1-2 min en tentatives.
_chain_failures: int = 0
_MAX_CHAIN_FAILURES = 3
#: Taille max de la vidéo envoyée en ligne (limite requête Gemini ~20 Mo).
_MAX_INLINE_BYTES = 15 * 1024 * 1024


# ---------------------------------------------------------------------------
# Modèles de données
# ---------------------------------------------------------------------------
class _Hook(BaseModel):
    text: str
    formula: str = ""


class _BriefSchema(BaseModel):
    """Schéma de sortie imposé au modèle (JSON mode typé)."""

    what_happens: str = Field(description="Ce qui se passe dans le clip, 1-2 phrases factuelles")
    score: int = Field(description="Potentiel viral 0-100 selon le barème")
    start: float = Field(description="Début conseillé du clip (s)")
    end: float = Field(description="Fin conseillée du clip (s)")
    hooks: list[_Hook] = Field(description="3 titres d'accroche, formules différentes")
    best: int = Field(description="Index (0-2) du meilleur titre")
    description: str = Field(description="Légende courte orientée recherche, sans hashtag")
    hashtags: list[str] = Field(description="3 à 5 hashtags de niche")


@dataclass
class ClipBrief:
    hook: str
    hashtags: list[str] = field(default_factory=list)
    description: str = ""
    #: None = LLM indisponible (on ne filtre pas sur la note).
    score: float | None = None
    start: float = 0.0
    end: float = 0.0
    what_happens: str = ""
    alternatives: list[str] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
_PROMPT = """Tu es le meilleur clippeur gaming de TikTok (des millions de vues sur des clips GTA).
Tu viens de REGARDER le clip vidéo ci-joint (image + son). Analyse-le puis prépare sa publication.

Contexte du clip :
- jeu : {game}
- streamer : {creator} ({platform})
- titre donné par le viewer qui a clippé : "{title}"
- {views} vues, durée {duration:.0f} s
{game_context}
{transcript_block}
1) `what_happens` : décris factuellement ce qui se passe (action à l'écran + réaction du streamer).

2) `score` (0-100) = potentiel viral RÉEL sur TikTok. Sois SÉVÈRE et utilise tout le barème :
   - 85-100 : on ne peut pas scroller — action folle, fail/bug hilarant, découverte inédite,
     réaction énorme, moment WTF compréhensible SANS contexte.
   - 65-84  : bon clip, l'action est claire, ça se regarde jusqu'au bout.
   - 40-64  : moyen — il faut du contexte, blague interne, ça démarre lentement.
   - 0-39   : il ne se passe rien — menu, écran de chargement, lecture du chat, pub/sponsor,
     streamer AFK, contenu sans rapport avec le jeu.

3) `start` / `end` (secondes, dans [0, {duration:.0f}]) : le passage À GARDER. Coupe le début mou
   (les 2 premières secondes doivent déjà accrocher) et ce qui traîne après la chute/réaction,
   mais garde la mise en place nécessaire pour comprendre. Clip final entre {clip_min} et
   {clip_max} s. Si tout est bon, renvoie start=0 et end={duration:.0f}.

4) `hooks` : 3 TITRES PUTACLIC affichés EN GROS sur la vidéo. Leur seul but : que le
   pouce S'ARRÊTE et que la personne regarde jusqu'au bout. Chaque titre utilise un levier
   DIFFÉRENT (indique-le dans `formula`) :
   · exagération / superlatif assumé ("Le bug le plus illégal de GTA 6")
   · choc / indignation ("Rockstar doit patcher ça d'urgence")
   · mystère / manque ("Personne n'a vu ce qui arrive à la fin")
   · défi au viewer ("99% des joueurs ratent ce détail", "Attends la fin…")
   · enjeu énorme / chiffre ("Il perd 2 millions en 3 secondes")
   · réaction ("Sa tête quand la police débarque")
   · POV / identification ("POV : tu lances GTA 6 pour la 1re fois")
   Règles pour CHAQUE titre :
   - {lang_rule} ; ton de TikTok, familier et direct, pas journalistique.
   - 3 à 8 mots, 50 caractères max : court = affiché plus gros = plus lisible.
   - accroché à un élément RÉEL du clip (action, véhicule, lieu, perso, réaction) : on
     EXAGÈRE ce qui se passe, on n'INVENTE pas ce qui ne se passe pas (sinon les viewers
     se sentent arnaqués, swipent et l'algo coule la vidéo).
   - ne révèle JAMAIS la chute : on promet, la vidéo délivre.
   - tu es le clippeur qui commente de l'extérieur : 3e personne, "tu" (POV) ou tournure
     neutre. INTERDIT : je, j', moi, mon, ma, mes, on, nous / I, I'm, my, me, we, us.
   - pas de guillemets, pas d'emoji ; fin en "?", "!" ou "…" autorisée.
   - mettre "{game_short}" dans le titre quand c'est naturel (les gens cherchent "{game_short}").
   - la catégorie du stream peut être fausse : si ce que tu vois n'est PAS {game_short}
     (autre jeu, serveur RP d'un autre GTA, discussion…), dis-le dans `what_happens` et
     n'écris ni "{game_short}" ni ses lieux/persos dans le titre.
   Titres récents de la chaîne — ne reprends NI leur structure NI leurs premiers mots :
{avoid}
   `best` = index (0, 1 ou 2) du titre le plus irrésistible à cliquer.

5) `description` : 1 phrase (max 150 caractères), {lang_short}, naturelle, avec les mots-clés
   que les gens tapent dans la recherche ({game_short}, le pseudo, le lieu/l'action). Sans hashtag.

6) `hashtags` : 3 à 5, de NICHE : jeu ({game_tag}), streamer (#{creator_tag}), sujet précis du
   clip, puis au plus 1 générique. Pas de liste de tags génériques.

Réponds UNIQUEMENT avec le JSON demandé."""


def _lang_bits(lang: str | None) -> tuple[str, str]:
    if (lang or "").lower().startswith("fr"):
        return ("écris impérativement en français (français parlé, naturel)", "en français")
    return ("write in English, punchy and natural (native TikTok English)", "in English")


def _game_short(game: str) -> str:
    tag = game_hashtag(game).lstrip("#")
    # "#GTA6" -> "GTA 6" (forme tapée dans la recherche)
    m = re.match(r"^([A-Za-z]+)(\d+)$", tag)
    return f"{m.group(1)} {m.group(2)}" if m else (game or "le jeu")


# ---------------------------------------------------------------------------
# Validation / normalisation (pur, testable)
# ---------------------------------------------------------------------------
_FIRST_PERSON = {
    "fr": re.compile(r"(?<!\w)(?:j['’]|(?:je|moi|mon|ma|mes|on|nous|notre|nos)(?!\w))", re.IGNORECASE),
    "en": re.compile(r"(?<!\w)(?:i['’]m|(?:i|im|my|me|we|our|us)(?!\w))", re.IGNORECASE),
}
#: Mots d'ouverture qui signent une FORMULE : deux titres qui commencent pareil
#: ("Quand…", "POV…") donnent l'effet "template" que l'algo et les viewers repèrent.
_OPENERS = {"quand", "pov", "when", "sa", "his", "her", "personne", "nobody",
            "ce", "cette", "this", "comment", "how", "attendez", "attends", "wait",
            "99", "rockstar"}
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]+"
)


def has_first_person(hook: str, lang: str | None) -> bool:
    key = "fr" if (lang or "").lower().startswith("fr") else "en"
    return bool(_FIRST_PERSON[key].search(hook or ""))


def clean_hook(text: str) -> str:
    t = _EMOJI.sub("", text or "")
    t = re.sub(r"[\"«»“”]", "", t)
    t = re.sub(r"#\w+", "", t)  # pas de hashtag dans le titre affiché
    t = " ".join(t.split()).strip(" ,;:-–—")
    t = re.sub(r"(?<![.…])\.$", "", t).rstrip(" ,;:-–—")  # point final retiré, "…" gardé
    return t


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def too_similar(hook: str, recent: list[str]) -> bool:
    """Même début (2 premiers mots) ou trop de mots en commun avec un titre récent."""
    w = _words(hook)
    if not w:
        return False
    for r in recent:
        rw = _words(r)
        if not rw:
            continue
        if w[:2] == rw[:2] or (w[0] == rw[0] and w[0] in _OPENERS):
            return True
        a, b = set(w), set(rw)
        if len(a & b) / max(1, len(a | b)) > 0.6:
            return True
    return False


def hook_problems(hook: str, lang: str | None, recent: list[str] | None = None) -> list[str]:
    """Liste des défauts d'un titre (vide = titre valide)."""
    problems = []
    n = len(hook.split())
    if n < 2 or n > 9:
        problems.append(f"{n} mots")
    if len(hook) > 65:
        problems.append(f"{len(hook)} caractères")
    if has_first_person(hook, lang):
        problems.append("1re personne")
    if recent and too_similar(hook, recent):
        problems.append("trop proche d'un titre récent")
    return problems


def pick_hook(candidates: list[str], best: int, lang: str | None, recent: list[str]) -> str | None:
    """Choisit le meilleur titre VALIDE (le préféré du modèle d'abord)."""
    cleaned = [clean_hook(c) for c in candidates]
    order = list(range(len(cleaned)))
    if 0 <= best < len(cleaned):
        order.remove(best)
        order.insert(0, best)
    for i in order:
        if cleaned[i] and not hook_problems(cleaned[i], lang, recent):
            return cleaned[i]
    # Tolérance : on accepte "proche d'un récent" / un peu long plutôt que rien,
    # mais JAMAIS la 1re personne.
    for i in order:
        if cleaned[i] and not has_first_person(cleaned[i], lang) and len(cleaned[i].split()) >= 2:
            return cleaned[i]
    return None


def normalize_hashtags(tags: list[str], game: str, creator: str, limit: int = 5) -> list[str]:
    """Jeu en tête, streamer ensuite, dédoublonnés (casse ignorée), plafonnés."""
    out: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = "#" + re.sub(r"[^\w]", "", (t or "").lstrip("#"))
        if len(t) < 3 or t.lower() in seen:
            return
        seen.add(t.lower())
        out.append(t)

    add(game_hashtag(game) if game else "#gaming")
    creator_tag = re.sub(r"\W", "", (creator or "").lower())
    if creator_tag:
        add(creator_tag)
    for t in tags or []:
        add(t)
    return out[:limit]


def clamp_trim(start: float, end: float, duration: float) -> tuple[float, float]:
    """Borne le recadrage proposé : on ne coupe jamais plus de 50 % du début,
    on respecte la durée min/max, et on ignore les retouches < 1 s."""
    if duration <= 0:
        return 0.0, duration
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        return 0.0, min(duration, settings.CLIP_MAX_DURATION_S)
    start = max(0.0, min(start, duration * 0.5))
    end = min(duration, max(end, start + settings.CLIP_MIN_DURATION_S))
    if end - start > settings.CLIP_MAX_DURATION_S:
        end = start + settings.CLIP_MAX_DURATION_S
    if start < 1.0:
        start = 0.0
    if duration - end < 1.0:
        end = duration
    if end - start < min(settings.CLIP_MIN_DURATION_S, duration):
        return 0.0, min(duration, settings.CLIP_MAX_DURATION_S)
    return round(start, 2), round(end, 2)


def fallback_hook(title: str, game: str, lang: str | None) -> str:
    """Titre de secours sans LLM : titre Twitch nettoyé (souvent bon, écrit par
    un viewer), sinon nom du jeu."""
    t = clean_hook(title)
    if t and not has_first_person(t, lang) and 2 <= len(t.split()) <= 9:
        return (t[0].upper() + t[1:])[:60]
    return f"Ce moment sur {_game_short(game)}" if (lang or "").startswith("fr") else f"This {_game_short(game)} moment"


# ---------------------------------------------------------------------------
# Appel Gemini
# ---------------------------------------------------------------------------
def _rate_limit() -> None:
    global _last_call_ts
    wait = settings.GEMINI_MIN_INTERVAL_S - (time.time() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


def _make_proxy(video: Path, out: Path) -> Path | None:
    """Copie basse définition (360p, 2 i/s, audio mono) pour l'envoi au LLM :
    quelques Mo au lieu de dizaines, sans perte utile (Gemini échantillonne à 1 i/s)."""
    cmd = [
        settings.FFMPEG_BIN, "-y", "-i", str(video),
        "-vf", "scale=-2:360,fps=2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
        "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        log.warning("Viral: proxy vidéo impossible (%s)", (proc.stderr or "")[-200:])
        return None
    if out.stat().st_size > _MAX_INLINE_BYTES:
        log.warning("Viral: proxy trop lourd (%d o) — envoi texte seul.", out.stat().st_size)
        return None
    return out


#: Santé des modèles PENDANT le cycle : un modèle retiré (404) ou qui échoue /
#: dépasse le délai 2 fois de suite est mis de côté (sinon chaque clip repaie
#: l'attente — constaté : flash surchargé = 3 à 11 min par appel).
_model_strikes: dict[str, int] = {}
_MAX_MODEL_STRIKES = 2


def _usable_models() -> list[str]:
    ok = [m for m in settings.GEMINI_MODELS if _model_strikes.get(m, 0) < _MAX_MODEL_STRIKES]
    return ok


def _generate(contents: list, temperature: float) -> tuple[_BriefSchema, str]:
    """Essaie chaque modèle encore sain de la chaîne. Retourne (brief, modèle)."""
    from google import genai
    from google.genai import errors, types

    if not settings.GEMINI_API_KEY:
        raise settings.ConfigError("GEMINI_API_KEY manquant")
    client = genai.Client(
        api_key=settings.GEMINI_API_KEY,
        # Délai dur par appel (ms) : au-delà on passe au modèle suivant.
        http_options=types.HttpOptions(timeout=int(settings.GEMINI_TIMEOUT_S * 1000)),
    )
    config = types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=_BriefSchema,
    )
    last_exc: Exception | None = None
    for model in _usable_models():
        for attempt in range(3):
            _rate_limit()
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                parsed = resp.parsed if isinstance(resp.parsed, _BriefSchema) else None
                if parsed is None:
                    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.text or "").strip())
                    parsed = _BriefSchema.model_validate(json.loads(raw))
                _model_strikes[model] = 0
                return parsed, model
            except errors.ClientError as exc:
                last_exc = exc
                if exc.code == 429 and attempt == 0:
                    log.info("Viral: %s quota/minute atteint — pause 35 s.", model)
                    time.sleep(35)
                    continue
                if exc.code == 404:  # modèle retiré : inutile de le réessayer
                    _model_strikes[model] = _MAX_MODEL_STRIKES
                else:
                    _model_strikes[model] = _model_strikes.get(model, 0) + 1
                log.warning("Viral: %s indisponible (%s) — modèle suivant.", model, exc.code)
                break
            except errors.ServerError as exc:
                last_exc = exc
                log.warning("Viral: %s erreur serveur (%s).", model, exc.code)
                if attempt < 2:  # surcharge passagère ("high demand") : on insiste un peu
                    time.sleep(5 if attempt == 0 else 15)
                    continue
                _model_strikes[model] = _model_strikes.get(model, 0) + 1
            except Exception as exc:  # noqa: BLE001 - délai dépassé, JSON invalide, réseau…
                last_exc = exc
                _model_strikes[model] = _model_strikes.get(model, 0) + 1
                log.warning("Viral: %s sans réponse exploitable (%s: %s) — modèle suivant.",
                            model, type(exc).__name__, str(exc)[:120])
                break
    raise RuntimeError(f"aucun modèle Gemini n'a répondu ({last_exc})")


def analyze_clip(
    video: str | Path,
    duration: float,
    *,
    title: str,
    creator: str,
    game: str,
    platform: str = "twitch",
    views: int = 0,
    lang: str | None = "en",
    recent_hooks: list[str] | None = None,
    transcript_text: str = "",
) -> ClipBrief:
    """Brief complet d'un clip. Ne lève jamais : repli sans LLM en cas d'échec."""
    recent = [h for h in (recent_hooks or []) if h][-15:]
    full = (0.0, round(duration, 2))
    base = ClipBrief(
        hook=fallback_hook(title, game, lang),
        hashtags=normalize_hashtags([], game, creator),
        start=full[0], end=min(full[1], float(settings.CLIP_MAX_DURATION_S)),
    )

    lang_rule, lang_short = _lang_bits(lang)
    info = info_for(game)
    prompt = _PROMPT.format(
        game=game or "jeu vidéo", creator=creator or "inconnu", platform=platform,
        title=(title or "")[:150], views=views, duration=duration,
        game_context=f"- contexte du jeu : {info.context}" if info.context else "",
        transcript_block=(
            f"\nTranscription (approximative) :\n{transcript_text[:1500]}\n" if transcript_text else ""
        ),
        clip_min=settings.CLIP_MIN_DURATION_S, clip_max=settings.CLIP_MAX_DURATION_S,
        lang_rule=lang_rule, lang_short=lang_short,
        avoid="\n".join(f"   - {h}" for h in recent) or "   (aucun)",
        game_short=_game_short(game), game_tag=game_hashtag(game) if game else "#gaming",
        creator_tag=re.sub(r"\W", "", (creator or "").lower()) or "streamer",
    )

    global _chain_failures
    if _chain_failures >= _MAX_CHAIN_FAILURES:
        log.info("Viral: LLM coupé pour ce cycle — titre de secours : %s", base.hook)
        return base
    try:
        from google.genai import types

        with tempfile.TemporaryDirectory() as td:
            contents: list = []
            if settings.GEMINI_SEND_VIDEO:
                proxy = _make_proxy(Path(video), Path(td) / "proxy.mp4")
                if proxy:
                    contents.append(types.Part.from_bytes(data=proxy.read_bytes(), mime_type="video/mp4"))
            contents.append(prompt)
            brief, model = _generate(contents, temperature=0.95)
        _chain_failures = 0
    except Exception as exc:  # noqa: BLE001 - le clip passe quand même, titre de secours
        _chain_failures += 1
        log.warning("Viral: brief LLM indisponible (%s) — titre de secours : %s", exc, base.hook)
        return base

    texts = [h.text for h in brief.hooks]
    hook = pick_hook(texts, brief.best, lang, recent) or base.hook
    start, end = clamp_trim(brief.start, brief.end, duration) if settings.SMART_TRIM else (0.0, duration)
    end = min(end, start + settings.CLIP_MAX_DURATION_S)
    out = ClipBrief(
        hook=hook,
        hashtags=normalize_hashtags(brief.hashtags, game, creator),
        description=clean_hook(brief.description)[:150],
        score=float(max(0, min(100, brief.score))),
        start=start,
        end=end,
        what_happens=brief.what_happens.strip()[:300],
        alternatives=[clean_hook(t) for t in texts if clean_hook(t) != hook],
        model=model,
    )
    log.info(
        "Viral: [%s] note=%.0f trim=%.1f-%.1f/%.1fs | %s | alt: %s",
        model, out.score, start, end, duration, hook, " / ".join(out.alternatives),
    )
    return out


def llm_down() -> bool:
    """True si le LLM a été coupé pour ce cycle (pannes répétées)."""
    return _chain_failures >= _MAX_CHAIN_FAILURES


def snap_to_words(start: float, end: float, words, duration: float) -> tuple[float, float]:
    """Ajuste les bornes pour ne jamais couper un mot en deux (temps relatifs au clip)."""
    for w in words:
        if w.start < start < w.end:
            start = max(0.0, w.start - 0.1)
        if w.start < end < w.end:
            end = min(duration, w.end + 0.15)
    return round(start, 2), round(end, 2)


if __name__ == "__main__":  # python -m src.viral.gemini <clip.mp4> [jeu] [fr|en]
    import sys

    from src.downloader.download import probe_media

    if len(sys.argv) < 2:
        print("usage: python -m src.viral.gemini <clip.mp4> [jeu] [fr|en]")
        raise SystemExit(2)
    dur, _, _ = probe_media(sys.argv[1])
    b = analyze_clip(
        sys.argv[1], dur, title="", creator="", platform="local",
        game=sys.argv[2] if len(sys.argv) > 2 else "Grand Theft Auto VI",
        lang=sys.argv[3] if len(sys.argv) > 3 else "fr",
    )
    print(json.dumps(b.to_dict(), ensure_ascii=False, indent=2))
