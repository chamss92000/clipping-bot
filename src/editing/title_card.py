"""Titre d'accroche au rendu *natif TikTok* (remplace l'ancien titre ASS "Word").

Rendu en PNG transparent avec Pillow (contrôle total du design), puis incrusté
par FFmpeg avec une animation d'entrée (glissé + fondu) — voir
`subtitles.burn_subtitles(title_png=...)`.

Deux styles :
  * ``box``     — le style texte TikTok reconnaissable : chaque ligne dans une
                  boîte blanche arrondie qui épouse le texte, texte noir en
                  TikTok Sans Black, mots "punch" en rouge TikTok, ombre douce.
  * ``outline`` — gros texte blanc MAJUSCULES à contour noir épais, mots punch
                  en jaune (style edit/motivation).

Police : TikTok Sans (police officielle de TikTok, OFL) embarquée dans
assets/fonts — rendu identique en local et en CI.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import settings
from src.utils.logging import get_logger

log = get_logger("editing.title_card")

_FONT_PATH = settings.FONTS_DIR / "TikTokSans.ttf"
_TIKTOK_RED = (254, 44, 85)
_YELLOW = (255, 221, 0)
_INK = (18, 18, 18)
_STRIP = ",.;:!?…«»\"'()[]-–—"

#: Mots "punch" mis en couleur (EN + FR) : curiosité, émotion, superlatifs, chiffres.
POWER_WORDS = {
    # EN
    "stop", "never", "now", "free", "secret", "best", "worst", "new", "money", "rich",
    "fast", "instantly", "everyone", "nobody", "everything", "nothing", "insane", "crazy",
    "hack", "mistake", "biggest", "only", "first", "last", "real", "truth", "wrong",
    "hidden", "banned", "exposed", "caught", "fired", "quit", "lost", "won", "rage",
    # FR
    "jamais", "personne", "tout", "rien", "secret", "choc", "fou", "folle", "dernier",
    "dernière", "première", "vraie", "vrai", "interdit", "viré", "quitte", "perd", "gagne",
    "craque", "explose", "hurle", "piège", "incroyable", "énorme", "seul", "seule",
    # Gaming / GTA
    "gta", "gta6", "gtavi", "vice", "leonida", "lucia", "jason", "police", "cops", "flics",
    "wanted", "bug", "glitch", "easter", "caché", "cachée", "braquage", "heist", "crash",
    "speedrun", "record", "1v5", "clutch",
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    """TikTok Sans en graisse Black. La police est variable et sa graisse PAR
    DÉFAUT est Light (300) : si on n'arrive pas à la passer en Black, on se
    rabat sur Anton plutôt que de produire un titre maigre et illisible."""
    try:
        f = ImageFont.truetype(str(_FONT_PATH), size)
        try:
            f.set_variation_by_name("Black")
            return f
        except Exception:  # noqa: BLE001
            f.set_variation_by_axes([36, 100, 900, 0])  # opsz, wdth, wght, slnt
            return f
    except Exception as exc:  # noqa: BLE001 - repli : police des sous-titres (déjà grasse)
        log.warning("TikTok Sans indisponible en Black (%s) — repli sur %s.", exc, settings.SUB_FONT)
        return ImageFont.truetype(str(settings.FONTS_DIR / f"{settings.SUB_FONT}-Regular.ttf"), size)


def _is_power(word: str) -> bool:
    w = word.strip(_STRIP).lower()
    return w in POWER_WORDS or any(c.isdigit() for c in w)


def _wrap(words: list[str], font, max_w: int, draw) -> list[list[str]]:
    lines: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def _balance(lines: list[list[str]], font, max_w: int, draw) -> list[list[str]]:
    """Évite une dernière ligne orpheline d'un seul mot (rendu plus pro)."""
    if len(lines) >= 2 and len(lines[-1]) == 1 and len(lines[-2]) >= 3:
        moved = lines[-2].pop()
        lines[-1].insert(0, moved)
        if draw.textlength(" ".join(lines[-1]), font=font) > max_w:  # ne tient pas : on annule
            lines[-2].append(lines[-1].pop(0))
    return lines


def render_title(text: str, out_png: str | Path, style: str | None = None) -> Path:
    """Rend le titre en PNG RGBA (taille auto). Retourne le chemin."""
    style = style or settings.TITLE_STYLE
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    text = " ".join((text or "").split())
    if style == "outline":
        text = text.upper()
    elif text:
        text = text[0].upper() + text[1:]
    words = text.split() or ["…"]

    size = settings.TITLE_CARD_FONT_SIZE
    font = _font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    max_text_w = int(settings.OUTPUT_WIDTH * settings.TITLE_CARD_MAX_WIDTH)
    lines = _balance(_wrap(words, font, max_text_w, probe), font, max_text_w, probe)
    # 2 lignes max : au-delà le titre mange l'écran et masque l'action/le visage.
    # On réduit la police jusqu'à tenir en 2 lignes (plancher lisible = 56px).
    while len(lines) > 2 and size > 56:
        size -= 4
        font = _font(size)
        lines = _balance(_wrap(words, font, max_text_w, probe), font, max_text_w, probe)

    ascent, descent = font.getmetrics()
    space = probe.textlength(" ", font=font)
    pad_x, pad_y = int(size * 0.34), int(size * (0.16 if style == "box" else 0.04))
    line_h = ascent + descent + 2 * pad_y
    overlap = int(size * 0.10) if style == "box" else int(size * 0.06)
    widths = [probe.textlength(" ".join(ln), font=font) for ln in lines]
    margin = int(size * (0.45 if style == "box" else 0.18))  # place pour l'ombre / le contour
    W = int(max(widths) + 2 * pad_x + 2 * margin)
    H = int(len(lines) * line_h - (len(lines) - 1) * overlap + 2 * margin)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    radius = int(size * 0.30)

    # Géométrie des boîtes (une par ligne, centrée)
    boxes = []
    y = margin
    for w in widths:
        bw = w + 2 * pad_x
        x0 = (W - bw) / 2
        boxes.append((x0, y, x0 + bw, y + line_h))
        y += line_h - overlap

    if style == "box":
        # Ombre douce sous les boîtes
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        off = int(size * 0.07)
        for (x0, y0, x1, y1) in boxes:
            sd.rounded_rectangle([x0, y0 + off, x1, y1 + off], radius=radius, fill=(0, 0, 0, 110))
        shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.12))
        img.alpha_composite(shadow)
        d = ImageDraw.Draw(img)
        for (x0, y0, x1, y1) in boxes:
            d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=(255, 255, 255, 255))
    d = ImageDraw.Draw(img)

    stroke = int(size * 0.11) if style == "outline" else 0
    base_color = _INK if style == "box" else (255, 255, 255)
    hl_color = _TIKTOK_RED if style == "box" else _YELLOW
    for ln, w, (x0, y0, x1, y1) in zip(lines, widths, boxes):
        x = x0 + pad_x
        baseline = y0 + pad_y + ascent
        for i, word in enumerate(ln):
            color = hl_color if (settings.TITLE_HIGHLIGHT and _is_power(word)) else base_color
            d.text((x, baseline), word, font=font, fill=color, anchor="ls",
                   stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += probe.textlength(word, font=font) + (space if i < len(ln) - 1 else 0)

    img.save(out_png)
    log.info("Titre: %s (%d ligne(s), %dpx, style=%s)", out_png.name, len(lines), size, style)
    return out_png


def title_center_y(mode: str, title_h: int = 0, face_box=None) -> int:
    """Hauteur (px) du CENTRE du titre, dans la zone sûre TikTok, SANS masquer le visage.

    Positions candidates par ordre de préférence selon le cadrage ; on garde la
    première qui ne chevauche pas le visage (boîte fournie par le cadrage) :
    * split : jointure facecam/gameplay, sinon haut, sinon début du gameplay
    * face  : tiers haut, sinon sous le visage
    * blur  : bande floutée du haut (aucun visage à éviter)
    Zone sûre : ~11% (onglets "Suivis | Pour toi") à ~62% (sous-titres + légende).
    """
    h = settings.OUTPUT_HEIGHT
    seam = h * settings.SPLIT_TOP_FRAC
    half = (title_h or int(h * 0.12)) / 2
    lo, hi = h * 0.11, h * 0.62
    if mode == "blur":
        fg_h = h * settings.BLUR_FG_HEIGHT_FRAC
        top_band = (h - fg_h) / 2
        return int(max(lo + half, min(top_band * 0.55, top_band - half - h * 0.01)))

    gap = h * 0.012
    top_most = lo + half
    candidates: list[float] = []
    if face_box is not None:
        _, fy0, _, fy1 = face_box
        above = fy0 - gap - half   # titre posé juste au-dessus de la tête
        below = fy1 + gap + half   # titre juste sous le visage
    else:
        above = below = None
    if mode == "split":
        candidates = [seam, below, above, top_most]
    else:  # face (talking head)
        candidates = [top_most, above, below]

    def fits(cy: float) -> bool:
        top, bot = cy - half, cy + half
        if top < lo or bot > hi:
            return False
        if face_box is None:
            return True
        return bot < face_box[1] - gap / 2 or top > face_box[3] + gap / 2

    for cy in candidates:
        if cy is not None and fits(cy):
            return int(cy)
    # Aucun emplacement parfait : tout en haut de la zone sûre (moindre mal).
    return int(top_most)


if __name__ == "__main__":  # python -m src.editing.title_card "Mon hook" [out.png] [box|outline]
    import sys

    t = sys.argv[1] if len(sys.argv) > 1 else "He lost everything in 3 seconds"
    o = sys.argv[2] if len(sys.argv) > 2 else str(settings.WORK_DIR / "title_test.png")
    s = sys.argv[3] if len(sys.argv) > 3 else None
    print(render_title(t, o, s))
