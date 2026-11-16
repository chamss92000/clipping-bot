"""Bloc 5 — Découpe + mise au format vertical 9:16.

Le rendu vertical est LE point qui fait qu'un clip est regardable ou non. On
choisit automatiquement, par clip, la meilleure mise en forme (recherche 2025
sur le clipping gaming/IRL vers TikTok/Shorts) :

  * ``split``  — une **facecam** (petite webcam dans un coin) est détectée :
                 on empile **facecam en haut** + **gameplay en bas**. C'est le
                 format qui marche le mieux pour du gaming : le spectateur voit
                 la réaction ET l'action en même temps.
  * ``face``   — un **gros visage plein cadre** (IRL / just chatting) : crop 9:16
                 **statique** centré sur le visage. Aucun panning => aucun
                 tremblement (c'était le défaut de l'ancien suivi de visage).
  * ``blur``   — **pas de visage fiable** (gameplay pur / cinématique) : image
                 entière centrée sur un fond flou (aucune tête coupée).

La décision se prend en échantillonnant les visages sur tout le clip puis en
regardant la taille MÉDIANE du visage :
    hauteur_visage / hauteur_source  >= FACE_BIG_RATIO   -> ``face``
                                     >= FACE_CAM_MIN_RATIO -> ``split``
                                     sinon (ou trop peu de frames)  -> ``blur``

Détecteur : MediaPipe (si dispo) -> cascade Haar OpenCV -> aucun (=> blur).
Tout le travail lourd est fait par FFmpeg (crop/scale/stack), jamais frame par
frame en Python.
"""

from __future__ import annotations

import statistics
import subprocess
import tempfile
from pathlib import Path

import cv2

from config import settings
from src.utils.logging import get_logger

log = get_logger("editing.clip")


class ClipError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Détection de visage (renvoie la BOÎTE du plus grand visage, pas juste x)
# ---------------------------------------------------------------------------
class _FaceDetector:
    """Retourne la boîte (x, y, w, h) en px du plus grand visage, ou None."""

    def __init__(self) -> None:
        self.backend = "none"
        self._mp = None
        self._haar = None
        self._init_mediapipe() or self._init_haar()
        log.info("Face detector: backend=%s", self.backend)

    def _init_mediapipe(self) -> bool:
        try:
            import mediapipe as mp

            self._mp = mp.solutions.face_detection.FaceDetection(
                model_selection=1,
                min_detection_confidence=settings.FACE_DETECTION_CONFIDENCE,
            )
            self.backend = "mediapipe"
            return True
        except Exception as exc:  # noqa: BLE001 - API mediapipe variable selon version
            log.debug("MediaPipe indisponible (%s), fallback Haar", exc)
            return False

    def _init_haar(self) -> bool:
        try:  # absent de certaines builds OpenCV (ex. 5.x sans contrib)
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._haar = cv2.CascadeClassifier(path)
        except Exception as exc:  # noqa: BLE001 - pas de détecteur => cadrage fond flou
            log.warning("Détecteur Haar indisponible (%s) — cadrage sans visage.", exc)
            self._haar = None
            self.backend = "none"
            return False
        if self._haar.empty():
            self.backend = "none"
            return False
        self.backend = "haar"
        return True

    def box(self, frame_bgr) -> tuple[float, float, float, float] | None:
        h, w = frame_bgr.shape[:2]
        if self.backend == "mediapipe":
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            res = self._mp.process(rgb)
            if not res.detections:
                return None
            best = max(
                res.detections,
                key=lambda d: d.location_data.relative_bounding_box.width
                * d.location_data.relative_bounding_box.height,
            )
            b = best.location_data.relative_bounding_box
            return (b.xmin * w, b.ymin * h, b.width * w, b.height * h)
        if self.backend == "haar":
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            faces = self._haar.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40)
            )
            if len(faces) == 0:
                return None
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            return (float(x), float(y), float(fw), float(fh))
        return None

    def close(self) -> None:
        if self._mp is not None:
            self._mp.close()


# ---------------------------------------------------------------------------
# Segmentation + échantillonnage des visages
# ---------------------------------------------------------------------------
def _extract_segment(source: Path, start: float, end: float, out: Path) -> Path:
    dur = max(0.1, end - start)
    cmd = [
        settings.FFMPEG_BIN, "-y",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
        "-c:a", "aac", "-b:a", "256k",
        str(out),
    ]
    _run_ffmpeg(cmd, "extraction segment")
    return out


def _sample_faces(video: Path):
    """Échantillonne la boîte du visage à FACE_SAMPLE_HZ.

    Retourne (boxes, src_w, src_h) où boxes est une liste de (x, y, w, h) | None.
    """
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ClipError(f"OpenCV ne peut pas ouvrir {video}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frame_count / fps if fps else 0

    detector = _FaceDetector()
    interval = 1.0 / max(0.5, settings.FACE_SAMPLE_HZ)
    boxes: list[tuple[float, float, float, float] | None] = []
    t = 0.0
    try:
        while duration == 0 or t <= duration:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            boxes.append(detector.box(frame))
            t += interval
    finally:
        detector.close()
        cap.release()
    return boxes, w, h


def _median_box(boxes) -> tuple[float, float, float, float] | None:
    """Boîte médiane (robuste aux faux positifs) des détections non nulles."""
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    xs = statistics.median(b[0] for b in valid)
    ys = statistics.median(b[1] for b in valid)
    ws = statistics.median(b[2] for b in valid)
    hs = statistics.median(b[3] for b in valid)
    return (xs, ys, ws, hs)


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------
def _run_ffmpeg(cmd: list[str], label: str, cwd: str | Path | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-800:]
        raise ClipError(f"FFmpeg ({label}) a échoué : {tail}")


def _even(v: float) -> int:
    i = int(round(v))
    return i - (i % 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def _blur_fit_graph(src_w: int = 16, src_h: int = 9) -> str:
    """Vidéo au centre sur un fond = elle-même floutée.

    Source paysage : la vidéo occupe BLUR_FG_HEIGHT_FRAC de la hauteur (bords
    légèrement rognés) au lieu d'être réduite à un mince bandeau (~32% de
    l'écran avec un simple "fit") — c'est ce qui rend un clip regardable sur
    TikTok. Source portrait/carrée : on la fait tenir entièrement.
    """
    w, h = settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT
    if src_h and src_w / src_h > 1.05:  # paysage
        fg_h = int(h * settings.BLUR_FG_HEIGHT_FRAC) // 2 * 2
        fg = f"[fg]scale=-2:{fg_h},crop='min(iw,{w})':{fg_h}[fgs];"
    else:
        fg = f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=28:2,eq=brightness=-0.12[bgb];"
        f"{fg}"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


#: Dernier mode de cadrage appliqué ("split" | "face" | "blur") — sert à placer
#: le titre au meilleur endroit (jointure, bande floutée, tiers haut).
LAST_MODE: str = "blur"
#: Boîte du visage (x0, y0, x1, y1) dans la VIDÉO FINALE 1080x1920, ou None —
#: permet au titre d'éviter de recouvrir le visage du streamer.
LAST_FACE_OUT: tuple[float, float, float, float] | None = None


def _map_box(face, crop_x: float, crop_y: float, sx: float, sy: float, dy: float = 0.0):
    fx, fy, fw, fh = face
    return ((fx - crop_x) * sx, (fy - crop_y) * sy + dy,
            (fx + fw - crop_x) * sx, (fy + fh - crop_y) * sy + dy)


def _split_graph(src_w: int, src_h: int, face) -> str:
    """Filtergraph split-stack : facecam (haut) + gameplay (bas).

    - haut  : région autour de la webcam (boîte visage agrandie), ratio = slot haut.
    - bas   : tranche centrale du gameplay, ratio = slot bas.
    Chaque région est croppée AU BON RATIO puis scalée exactement au slot
    (aucune déformation, aucune bande noire).
    """
    ow, oh = settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT
    top_h = _even(oh * settings.SPLIT_TOP_FRAC)
    bot_h = oh - top_h
    fx, fy, fw, fh = face
    fcx, fcy = fx + fw / 2, fy + fh / 2

    # --- Facecam (haut) : boîte visage agrandie, au ratio du slot haut ---
    top_ar = ow / top_h
    # largeur = max(zoom autour du visage, plancher anti-cadrage-trop-serré)
    cam_w = _clamp(
        max(fw * settings.CAM_ZOOM, src_w * settings.CAM_MIN_WIDTH_FRAC), 80, src_w
    )
    cam_h = cam_w / top_ar
    if cam_h > src_h:
        cam_h = src_h
        cam_w = cam_h * top_ar
    cam_x = _clamp(fcx - cam_w / 2, 0, src_w - cam_w)
    cam_y = _clamp(fcy - cam_h / 2, 0, src_h - cam_h)
    cam_w, cam_h = _even(cam_w), _even(cam_h)
    cam_x, cam_y = _even(cam_x), _even(cam_y)

    # --- Gameplay (bas) : plus grande tranche centrée au ratio du slot bas ---
    bot_ar = ow / bot_h
    if src_w / src_h > bot_ar:  # source plus large que le slot : on rogne en largeur
        game_h = _even(src_h)
        game_w = _even(src_h * bot_ar)
    else:
        game_w = _even(src_w)
        game_h = _even(src_w / bot_ar)
    game_x = _even(_clamp((src_w - game_w) / 2, 0, src_w - game_w))
    game_y = _even(_clamp((src_h - game_h) / 2, 0, src_h - game_h))

    global LAST_FACE_OUT
    LAST_FACE_OUT = _map_box(face, cam_x, cam_y, ow / cam_w, top_h / cam_h)

    return (
        f"[0:v]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
        f"scale={ow}:{top_h},setsar=1[top];"
        f"[0:v]crop={game_w}:{game_h}:{game_x}:{game_y},"
        f"scale={ow}:{bot_h},setsar=1[bot];"
        f"[top][bot]vstack,setsar=1[v]"
    )


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def _decide_mode(boxes, src_w: int, src_h: int) -> tuple[str, tuple | None]:
    """Choisit le mode de cadrage et renvoie (mode, boîte médiane|None)."""
    forced = settings.FRAMING
    med = _median_box(boxes)
    rate = (sum(1 for b in boxes if b is not None) / len(boxes)) if boxes else 0.0

    if forced in ("split", "face", "blur"):
        if forced in ("split", "face") and med is None:
            log.info("Clip: cadrage %s forcé mais aucun visage -> fond flou.", forced)
            return "blur", None
        return forced, med

    # --- auto ---
    if med is None or rate < settings.FACE_MIN_RATE:
        log.info("Clip: cadrage=blur (visage sur %.0f%% des frames < seuil)", rate * 100)
        return "blur", None

    fh_ratio = med[3] / src_h
    # Position du visage : un vrai "talking head" (IRL/just chatting) est au
    # centre du cadre ; une facecam posée sur du gameplay est en bord/coin.
    # Sans ce test, une grosse facecam en bas d'écran partait en crop "visage"
    # (gameplay rogné, visage coupé) au lieu d'un split-stack.
    cx = (med[0] + med[2] / 2) / src_w
    cy = (med[1] + med[3] / 2) / src_h
    central = 0.28 <= cx <= 0.72 and 0.12 <= cy <= 0.62
    if fh_ratio >= settings.FACE_BIG_RATIO and central:
        mode = "face"
    elif fh_ratio >= settings.FACE_CAM_MIN_RATIO:
        mode = "split"
    else:
        mode = "blur"
    log.info(
        "Clip: cadrage=%s (visage %.0f%% des frames, hauteur=%.1f%%, centre=(%.2f,%.2f))",
        mode, rate * 100, fh_ratio * 100, cx, cy,
    )
    return mode, med


def make_vertical_clip(
    source_path: str | Path,
    start: float,
    end: float,
    out_path: str | Path,
) -> Path:
    """Découpe [start,end] et met au format 9:16 (mode choisi automatiquement)."""
    source_path = Path(source_path).resolve()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path = out_path.resolve()
    ow, oh = settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT

    global LAST_FACE_OUT
    LAST_FACE_OUT = None
    with tempfile.TemporaryDirectory() as td:
        seg = _extract_segment(source_path, start, end, Path(td) / "seg.mp4")
        boxes, w, h = _sample_faces(seg)
        mode, med = _decide_mode(boxes, w, h)

        common_tail = [
            "-map", "[v]", "-map", "0:a?",
            "-r", str(settings.OUTPUT_FPS),
            # Intermédiaire quasi sans perte : la compression finale se fait UNE
            # fois (burn_subtitles), pas 3 fois en cascade.
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
            "-c:a", "aac", "-b:a", "256k",
            str(out_path),
        ]

        if mode == "split" and med is not None:
            graph = _split_graph(w, h, med)
            cmd = [settings.FFMPEG_BIN, "-y", "-i", str(seg), "-filter_complex", graph, *common_tail]
            _run_ffmpeg(cmd, "split-stack facecam+gameplay", cwd=td)

        elif mode == "face" and med is not None:
            # Crop 9:16 STATIQUE centré sur le visage (aucun panning).
            target_ar = ow / oh
            crop_h = _even(h)
            crop_w = _even(min(w, h * target_ar))
            fcx = med[0] + med[2] / 2
            crop_x = _even(_clamp(fcx - crop_w / 2, 0, w - crop_w))
            LAST_FACE_OUT = _map_box(med, crop_x, 0, ow / crop_w, oh / crop_h)
            graph = (
                f"[0:v]crop={crop_w}:{crop_h}:{crop_x}:0,"
                f"scale={ow}:{oh},setsar=1[v]"
            )
            cmd = [settings.FFMPEG_BIN, "-y", "-i", str(seg), "-filter_complex", graph, *common_tail]
            _run_ffmpeg(cmd, "crop statique visage", cwd=td)

        else:  # blur
            cmd = [
                settings.FFMPEG_BIN, "-y", "-i", str(seg),
                "-filter_complex", _blur_fit_graph(w, h), *common_tail,
            ]
            _run_ffmpeg(cmd, "cadrage fond flou", cwd=td)

    global LAST_MODE
    LAST_MODE = mode
    log.info("Clip: écrit %s (%dx%d, mode=%s)", out_path.name, ow, oh, mode)
    return out_path


if __name__ == "__main__":  # python -m src.editing.clip <source> <start> <end> [out]
    import sys

    if len(sys.argv) < 4:
        print("usage: python -m src.editing.clip <source> <start> <end> [out.mp4]")
        raise SystemExit(2)
    src = sys.argv[1]
    s, e = float(sys.argv[2]), float(sys.argv[3])
    out = sys.argv[4] if len(sys.argv) >= 5 else str(settings.CLIPS_DIR / "clip_test.mp4")
    print(make_vertical_clip(src, s, e, out))
