"""Générateur de vidéos "cute / relax" (style thumb-game) — 100% code, 0€.

Rendu d'une vidéo verticale 1080x1920 en boucle :
  * fond pastel doux,
  * un personnage mignon (poussin / chat) qui respire (idle bob),
  * un tracé animé (boucle en pointillés) + un point guide qui tourne, que le
    spectateur suit avec le doigt,
  * un texte d'accroche bilingue (FR + EN) en haut.

Aucune musique embarquée (droits) : la vidéo est muette, l'utilisateur ajoute un
son tendance directement dans TikTok au moment de poster (meilleur pour l'algo).

Technique : on dessine chaque frame avec Pillow en supersampling ×2 (bords
lisses), on redimensionne, et on pousse les frames brutes dans FFmpeg via stdin
(pas de milliers de PNG sur le disque). Tout est déterministe (seed) et bouclable
(les animations ont une période qui divise la durée => boucle sans coupure).
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
#: On dessine à la résolution finale (rapide) et on lisse les bords avec un léger
#: flou (moins coûteux que le supersampling ×2, qui était trop lent).
SS = 1
_SMOOTH = 0.8  # rayon du flou anti-aliasing (px)


# --- Contenu (varié à chaque vidéo) ----------------------------------------
# (fond, accent/texte, couleur du tracé)
THEMES = [
    ("#DCEEFB", "#2C6FB5", "#7FB2E5"),  # ciel
    ("#F6D96B", "#7A5300", "#E0A030"),  # jaune chaud
    ("#FCD9E5", "#B23A6E", "#F49CC0"),  # rose
    ("#D9F2E6", "#1F7A5A", "#7FD1AE"),  # menthe
    ("#E7E0FB", "#5B3FA8", "#B0A0E8"),  # lavande
]

# (personnage, couleur principale)
CHARACTERS = ["chick", "cat"]

# (texte FR, texte EN)
PROMPTS = [
    ("Suis le tracé avec ton doigt", "Follow the line with your finger"),
    ("Pose ton pouce sur l'écran", "Place your thumb on the screen"),
    ("Détends-toi et respire", "Relax and breathe"),
    ("Reste concentré jusqu'au bout", "Stay focused till the end"),
    ("Fais tourner ton doigt en rythme", "Move your finger to the beat"),
    ("Garde le rythme, ne lâche pas", "Keep the rhythm, don't stop"),
]

FPS = 20
DURATION_S = 10  # multiple des périodes d'anim => boucle parfaite


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font(size_px: int) -> ImageFont.FreeTypeFont:
    path = settings.FONTS_DIR / f"{settings.SUB_FONT}-Regular.ttf"
    try:
        return ImageFont.truetype(str(path), size_px)
    except Exception:  # noqa: BLE001 - repli police par défaut
        return ImageFont.load_default()


def _draw_chick(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, main) -> None:
    beak = _hex("#F2A03D")
    black = (40, 40, 40)
    blush = (255, 150, 160)
    # pattes
    d.line([(cx - r // 3, cy + r), (cx - r // 3, cy + int(r * 1.25))], fill=beak, width=max(2, r // 12))
    d.line([(cx + r // 3, cy + r), (cx + r // 3, cy + int(r * 1.25))], fill=beak, width=max(2, r // 12))
    # corps
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=main)
    # ailes
    d.ellipse([cx - r, cy - r // 3, cx - r // 3, cy + r // 2], fill=main)
    d.ellipse([cx + r // 3, cy - r // 3, cx + r, cy + r // 2], fill=main)
    # touffe
    for dx in (-r // 6, 0, r // 6):
        d.line([(cx + dx, cy - r), (cx + dx, cy - int(r * 1.3))], fill=main, width=max(2, r // 14))
    # yeux
    er = max(3, r // 9)
    d.ellipse([cx - r // 3 - er, cy - er, cx - r // 3 + er, cy + er], fill=black)
    d.ellipse([cx + r // 3 - er, cy - er, cx + r // 3 + er, cy + er], fill=black)
    # bec
    d.polygon([(cx - r // 8, cy + r // 5), (cx + r // 8, cy + r // 5), (cx, cy + r // 2)], fill=beak)
    # joues
    d.ellipse([cx - int(r * 0.7), cy + r // 8, cx - int(r * 0.4), cy + int(r * 0.4)], fill=blush)
    d.ellipse([cx + int(r * 0.4), cy + r // 8, cx + int(r * 0.7), cy + int(r * 0.4)], fill=blush)


def _draw_cat(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, main) -> None:
    black = (35, 35, 35)
    blush = (255, 140, 150)
    gold = (245, 205, 90)
    # oreilles
    d.polygon([(cx - r, cy - r // 2), (cx - r // 2, cy - int(r * 1.3)), (cx - r // 6, cy - r // 2)], fill=main)
    d.polygon([(cx + r // 6, cy - r // 2), (cx + r // 2, cy - int(r * 1.3)), (cx + r, cy - r // 2)], fill=main)
    # corps
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=main)
    # yeux (grands, avec reflet)
    er = max(5, r // 4)
    for sx in (-r // 2, r // 2):
        d.ellipse([cx + sx - er, cy - er, cx + sx + er, cy + er], fill=gold)
        pr = er // 2
        d.ellipse([cx + sx - pr, cy - pr, cx + sx + pr, cy + pr], fill=black)
        hr = max(1, er // 5)
        d.ellipse([cx + sx - hr, cy - er // 2 - hr, cx + sx + hr, cy - er // 2 + hr], fill=(255, 255, 255))
    # museau
    d.polygon([(cx - r // 12, cy + r // 4), (cx + r // 12, cy + r // 4), (cx, cy + r // 3)], fill=(90, 70, 70))
    # joues
    d.ellipse([cx - int(r * 0.75), cy + r // 6, cx - int(r * 0.45), cy + int(r * 0.42)], fill=blush)
    d.ellipse([cx + int(r * 0.45), cy + r // 6, cx + int(r * 0.75), cy + int(r * 0.42)], fill=blush)


def _draw_character(d, name, cx, cy, r, main):
    (_draw_cat if name == "cat" else _draw_chick)(d, cx, cy, r, main)


def _loop_point(px, py, rx, ry, frac):
    a = 2 * math.pi * frac
    return (px + rx * math.cos(a), py + ry * math.sin(a))


def _draw_frame(t: float, cfg: dict) -> Image.Image:
    bw, bh = W * SS, H * SS
    img = Image.new("RGB", (bw, bh), _hex(cfg["bg"]))
    d = ImageDraw.Draw(img)
    accent = _hex(cfg["accent"])
    path_c = _hex(cfg["path"])

    # --- tracé en pointillés (boucle) + point guide ---
    px, py = bw // 2, int(bh * 0.60)
    rx, ry = int(bw * 0.30), int(bh * 0.16)
    n = 72
    phase = (t / DURATION_S) * n  # défile d'un tour de motif sur la durée => loop
    for i in range(n):
        if (i + int(phase)) % 2 == 0:
            continue
        x1, y1 = _loop_point(px, py, rx, ry, i / n)
        x2, y2 = _loop_point(px, py, rx, ry, (i + 0.5) / n)
        d.line([(x1, y1), (x2, y2)], fill=path_c, width=max(3, SS * 5))
    gx, gy = _loop_point(px, py, rx, ry, (t / DURATION_S) % 1.0)  # 1 tour / durée => loop
    gr = SS * 26
    d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=(255, 255, 255))
    d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], outline=accent, width=SS * 5)
    ir = gr // 2
    d.ellipse([gx - ir, gy - ir, gx + ir, gy + ir], fill=accent)

    # --- personnage (respire) ---
    bob = math.sin(2 * math.pi * (t / 2.0)) * (SS * 10)  # période 2s (12/2=6 cycles) => loop
    r = int(bw * 0.16)
    _draw_character(d, cfg["character"], bw // 2, int(bh * 0.34 + bob), r, _hex(cfg["main"]))

    # --- textes ---
    f1 = _font(int(bh * 0.045))
    f2 = _font(int(bh * 0.032))
    stroke = max(4, SS * 4)
    for text, font, y in (
        (cfg["fr"], f1, int(bh * 0.09)),
        (cfg["en"], f2, int(bh * 0.145)),
    ):
        tb = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
        tw = tb[2] - tb[0]
        d.text(((bw - tw) // 2, y), text, font=font, fill=accent,
               stroke_width=stroke, stroke_fill=(255, 255, 255))

    if SS != 1:
        img = img.resize((W, H), Image.LANCZOS)
    if _SMOOTH:
        img = img.filter(ImageFilter.GaussianBlur(_SMOOTH))
    return img


def make_relax_video(out_path: str | Path, seed: int | None = None) -> Path:
    """Génère une vidéo relax déterministe (seed) dans out_path. Retourne le chemin."""
    rng = random.Random(seed)
    bg, accent, path_c = rng.choice(THEMES)
    fr, en = rng.choice(PROMPTS)
    cfg = {
        "bg": bg, "accent": accent, "path": path_c,
        "character": rng.choice(CHARACTERS),
        "main": rng.choice(["#FFD84D", "#FFFFFF", "#8A8A8A", "#FF9F68", "#B0E0A8"]),
        "fr": fr, "en": en,
    }
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

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
    log.info("Relax: rendu %d frames (%s / %s)…", total, cfg["character"], cfg["fr"])
    # stderr -> fichier (jamais un PIPE non drainé pendant l'écriture : ça
    # remplit le buffer OS et fige ffmpeg — deadlock).
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        try:
            for i in range(total):
                proc.stdin.write(_draw_frame(i / FPS, cfg).tobytes())
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
