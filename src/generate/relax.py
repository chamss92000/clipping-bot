"""Générateur de vidéos "thumb-game" (style doodle) — 100% code, 0€.

Inspiré des vidéos relax coréennes : un écran pastel avec un GROS POUCE dessiné
(contour noir, style doodle) qui se balance, que le spectateur suit avec son
propre pouce, + un texte manuscrit en anglais avec une petite ligne en coréen
(police Gaegu, qui gère les deux). Quelques cœurs qui montent pour l'ambiance.

Rendu : Pillow (dessin) + FFmpeg (encodage via pipe). Muet volontairement
(l'utilisateur ajoute un son tendance TikTok au post). Déterministe (seed),
bouclable (les périodes divisent la durée => boucle sans coupure).

⚠️ FFmpeg : stderr redirigé vers un fichier (jamais un PIPE non drainé pendant
l'écriture des frames => deadlock).
"""

from __future__ import annotations

import math
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import settings
from src.utils.logging import get_logger

log = get_logger("generate.relax")

W, H = settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT
FPS = 20
DURATION_S = 10          # multiple des périodes d'anim => boucle parfaite
_SMOOTH = 0.7            # léger flou anti-aliasing
INK = (38, 38, 38)       # encre noire (contours + texte)

_FONT = settings.FONTS_DIR / "Gaegu-Bold.ttf"

# Fonds pastel façon doodle coréen (bleu, menthe, lavande, pêche, rose).
THEMES = ["#BFD3F2", "#C7ECE4", "#DED3F5", "#FBE0C8", "#FBD5E0"]

# (anglais = principal, coréen = petite touche)
PROMPTS = [
    ("MOVE YOUR FINGER WITH THE SCREEN", "화면을 따라 손가락을 움직이세요"),
    ("FOLLOW MY THUMB", "엄지를 따라오세요"),
    ("CAN YOU KEEP UP?", "따라올 수 있나요?"),
    ("DON'T LOSE THE RHYTHM", "리듬을 놓치지 마세요"),
    ("STAY WITH ME TILL THE END", "끝까지 함께해요"),
    ("JUST RELAX AND FOLLOW", "편하게 따라 해보세요"),
]


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font(size_px: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(_FONT), size_px)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _fit_font(d: ImageDraw.ImageDraw, text: str, max_w: int, start_px: int, stroke: int):
    """Réduit la taille jusqu'à ce que le texte tienne dans max_w (px)."""
    size = start_px
    while size > 12:
        f = _font(size)
        tb = d.textbbox((0, 0), text, font=f, stroke_width=stroke)
        if tb[2] - tb[0] <= max_w:
            return f
        size -= 3
    return _font(size)


def _draw_thumb_layer(length: int, width: int) -> Image.Image:
    """Dessine un pouce (blanc, contour noir) sur un calque, base au CENTRE du
    calque (pour pouvoir le faire pivoter autour de la base)."""
    lw, lh = length, length * 2
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, base_y = lw // 2, lh // 2  # base = centre du calque
    top_y = base_y - length
    out = max(6, int(width * 0.10))
    skin = (255, 255, 255)

    # doigt (capsule)
    d.rounded_rectangle(
        [cx - width // 2, top_y, cx + width // 2, base_y],
        radius=width // 2, fill=skin, outline=INK, width=out,
    )
    # ongle
    nw, nh = int(width * 0.62), int(width * 0.9)
    ny = top_y + int(width * 0.35)
    d.rounded_rectangle(
        [cx - nw // 2, ny, cx + nw // 2, ny + nh],
        radius=nw // 2, outline=INK, width=max(3, out // 2),
    )
    # pli de phalange
    ky = base_y - int(length * 0.42)
    d.arc([cx - width // 2, ky - width // 3, cx + width // 2, ky + width // 3],
          200, 340, fill=INK, width=max(3, out // 2))
    return layer


def _draw_heart(d: ImageDraw.ImageDraw, cx: int, cy: int, s: int, fill) -> None:
    r = s // 2
    d.ellipse([cx - s // 2, cy - r, cx, cy], fill=fill, outline=INK, width=max(2, s // 12))
    d.ellipse([cx, cy - r, cx + s // 2, cy], fill=fill, outline=INK, width=max(2, s // 12))
    d.polygon([(cx - s // 2, cy - r // 6), (cx + s // 2, cy - r // 6), (cx, cy + r)],
              fill=fill)
    d.line([(cx - s // 2, cy - r // 6), (cx, cy + r)], fill=INK, width=max(2, s // 12))
    d.line([(cx + s // 2, cy - r // 6), (cx, cy + r)], fill=INK, width=max(2, s // 12))


def _draw_frame(t: float, cfg: dict, thumb: Image.Image) -> Image.Image:
    img = Image.new("RGB", (W, H), _hex(cfg["bg"]))
    d = ImageDraw.Draw(img)

    # --- cœurs qui montent (ambiance, boucle) ---
    for k in range(4):
        frac = ((t / DURATION_S) + k / 4.0) % 1.0
        hy = int(H * 0.9 - frac * H * 0.55)
        hx = int(W * (0.2 + 0.15 * k) + math.sin(frac * 6.28 + k) * 20)
        s = int(W * 0.05)
        _draw_heart(d, hx, hy, s, _hex("#F1808F"))

    # --- pouce qui se balance (le "jeu") ---
    angle = math.sin(2 * math.pi * (t / 2.5)) * 14.0  # période 2.5s (10/2.5=4) => loop
    rot = thumb.rotate(angle, resample=Image.BICUBIC, expand=False)
    bx, by = int(W * 0.54), int(H * 1.04)  # base près du bas de l'écran
    img.paste(rot, (bx - rot.width // 2, by - rot.height // 2), rot)

    # --- textes (anglais principal + petite ligne coréenne), auto-ajustés ---
    stroke = max(3, int(H * 0.004))
    max_w = int(W * 0.90)
    f_ko = _fit_font(d, cfg["ko"], max_w, int(H * 0.032), stroke)
    f_en = _fit_font(d, cfg["en"], max_w, int(H * 0.058), stroke)
    for text, font, y in ((cfg["ko"], f_ko, int(H * 0.06)), (cfg["en"], f_en, int(H * 0.10))):
        tb = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
        d.text(((W - (tb[2] - tb[0])) // 2, y), text, font=font, fill=INK,
               stroke_width=stroke, stroke_fill=(255, 255, 255))

    if _SMOOTH:
        img = img.filter(ImageFilter.GaussianBlur(_SMOOTH))
    return img


def make_relax_video(out_path: str | Path, seed: int | None = None) -> Path:
    """Génère une vidéo thumb-game déterministe (seed). Retourne le chemin."""
    rng = random.Random(seed)
    en, ko = rng.choice(PROMPTS)
    cfg = {"bg": rng.choice(THEMES), "en": en, "ko": ko}
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    thumb = _draw_thumb_layer(int(H * 0.58), int(W * 0.24))

    cmd = [
        settings.FFMPEG_BIN, "-y", "-loglevel", "error", "-nostats",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-shortest", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        str(out_path),
    ]
    total = FPS * DURATION_S
    log.info("Relax: rendu %d frames (%s)…", total, cfg["en"])
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        try:
            for i in range(total):
                proc.stdin.write(_draw_frame(i / FPS, cfg, thumb).tobytes())
        finally:
            proc.stdin.close()
            code = proc.wait()
        if code != 0:
            errf.seek(0)
            raise RuntimeError(f"FFmpeg (relax) a échoué : {errf.read().decode(errors='ignore')[-600:]}")
    log.info("Relax: vidéo écrite %s", out_path.name)
    return out_path


if __name__ == "__main__":  # python -m src.generate.relax [out.mp4] [seed]
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else str(settings.WORK_DIR / "relax_test.mp4")
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print(make_relax_video(out, seed))
