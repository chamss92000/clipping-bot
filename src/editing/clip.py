"""Bloc 5 — Découpe + recadrage vertical 9:16 avec suivi de visage.

Pipeline d'un clip :
  1. Extraction précise du segment [start, end] (FFmpeg, ré-encodage → pas de
     décalage de keyframe).
  2. Échantillonnage de la position du visage à FACE_SAMPLE_HZ (OpenCV +
     détecteur), puis lissage de la trajectoire horizontale du cadre.
  3. Recadrage 9:16 **animé** via FFmpeg `sendcmd` sur le filtre `crop`
     (le cadre suit le visage) → scale 1080x1920. Aucune boucle d'encodage
     frame-par-frame en Python : FFmpeg fait tout le travail lourd.
  4. Fallback : crop centré statique si aucun visage détecté.

Détecteur de visage à dégradation progressive :
  MediaPipe (si dispo) → cascade Haar OpenCV → crop centré.
"""

from __future__ import annotations

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
# Détection de visage (abstraction avec fallback)
# ---------------------------------------------------------------------------
class _FaceDetector:
    """Retourne le centre X (px) du plus grand visage d'une frame BGR, ou None."""

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
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._haar = cv2.CascadeClassifier(path)
        if self._haar.empty():
            self.backend = "none"
            return False
        self.backend = "haar"
        return True

    def center_x(self, frame_bgr) -> float | None:
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
            box = best.location_data.relative_bounding_box
            return (box.xmin + box.width / 2) * w
        if self.backend == "haar":
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            faces = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            if len(faces) == 0:
                return None
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            return x + fw / 2
        return None

    def close(self) -> None:
        if self._mp is not None:
            self._mp.close()


# ---------------------------------------------------------------------------
# Segmentation + échantillonnage
# ---------------------------------------------------------------------------
def _extract_segment(source: Path, start: float, end: float, out: Path) -> Path:
    dur = max(0.1, end - start)
    cmd = [
        settings.FFMPEG_BIN, "-y",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    _run_ffmpeg(cmd, "extraction segment")
    return out


def _sample_face_track(video: Path) -> tuple[list[tuple[float, float | None]], int, int]:
    """Échantillonne le centre X du visage à FACE_SAMPLE_HZ. Retourne (samples, w, h)."""
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
    samples: list[tuple[float, float | None]] = []
    t = 0.0
    try:
        while duration == 0 or t <= duration:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            samples.append((t, detector.center_x(frame)))
            t += interval
    finally:
        detector.close()
        cap.release()
    return samples, w, h


def _smooth_track(
    samples: list[tuple[float, float | None]], src_w: int, crop_w: int, window: int = 5
) -> list[tuple[float, int]] | None:
    """Comble les trous, lisse (moyenne glissante), clampe x dans [0, src_w-crop_w].

    Retourne None si aucun visage détecté (=> caller fait un crop centré statique).
    """
    xs = [x for _, x in samples]
    if all(x is None for x in xs):
        return None

    max_x = src_w - crop_w
    center = max_x / 2

    # 1) Rejet des détections aberrantes (Haar sort parfois un faux visage très
    #    loin) : on ignore un point qui saute de plus de 35% de la largeur d'un
    #    échantillon au suivant — on garde la dernière valeur fiable.
    face_centers: list[float] = []
    last_valid: float | None = None
    jump_limit = src_w * 0.35
    for x in xs:
        if x is None:
            face_centers.append(last_valid if last_valid is not None else src_w / 2)
        elif last_valid is not None and abs(x - last_valid) > jump_limit:
            face_centers.append(last_valid)  # saut trop grand => ignoré
        else:
            face_centers.append(x)
            last_valid = x

    # 2) Cadre centré sur le visage, borné.
    targets = [min(max(fx - crop_w / 2, 0.0), max_x) for fx in face_centers]

    # 3) Lissage FORT : moyenne exponentielle (pan lent) + limitation de vitesse
    #    (max de déplacement par échantillon) => plus de saccades.
    alpha = 0.18                          # plus petit = plus lisse
    max_step = max(2.0, src_w * 0.012)    # px max entre 2 échantillons (~4 Hz)
    smoothed: list[float] = []
    s = targets[0]
    for tgt in targets:
        s = alpha * tgt + (1 - alpha) * s
        if smoothed:
            delta = s - smoothed[-1]
            if delta > max_step:
                s = smoothed[-1] + max_step
            elif delta < -max_step:
                s = smoothed[-1] - max_step
        smoothed.append(s)

    # 4) clamp + arrondi pair (yuv420 exige des dimensions/positions paires)
    out: list[tuple[float, int]] = []
    for (t, _), x in zip(samples, smoothed):
        cx = int(max(0, min(x, max_x)))
        cx -= cx % 2
        out.append((t, cx))
    return out


def _write_sendcmd(track: list[tuple[float, int]], path: Path) -> None:
    lines = [f"{t:.3f} crop x {x};" for t, x in track]
    path.write_text("\n".join(lines), encoding="utf-8")


def _blur_fit_graph() -> str:
    """Filtergraph "image entière + fond flou" (rendu propre sur gameplay/cinéma).

    L'image source est mise à l'échelle pour tenir entièrement dans le 9:16, et
    le fond est la même image agrandie/rognée puis floutée et légèrement
    assombrie (contraste avec les sous-titres).
    """
    w, h = settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},boxblur=24:2,eq=brightness=-0.08[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


def _run_ffmpeg(cmd: list[str], label: str, cwd: str | Path | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-800:]
        raise ClipError(f"FFmpeg ({label}) a échoué : {tail}")


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def make_vertical_clip(
    source_path: str | Path,
    start: float,
    end: float,
    out_path: str | Path,
) -> Path:
    """Découpe [start,end], recadre en 9:16 avec suivi de visage, écrit out_path."""
    source_path = Path(source_path).resolve()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Absolu : FFmpeg tourne avec cwd=dossier temp pour le recadrage animé.
    out_path = out_path.resolve()

    with tempfile.TemporaryDirectory() as td:
        seg = _extract_segment(source_path, start, end, Path(td) / "seg.mp4")

        samples, w, h = _sample_face_track(seg)

        # --- Choix du cadrage -------------------------------------------------
        # Un gros plan "suivi de visage" n'a de sens que s'il y a vraiment un
        # visage. Sur du gameplay/cinématique, il montre surtout du décor vide :
        # dans ce cas l'image entière sur fond flou rend bien mieux.
        detected = sum(1 for _, x in samples if x is not None)
        rate = detected / len(samples) if samples else 0.0
        mode = settings.FRAMING
        if mode == "auto":
            mode = "face" if rate >= settings.FACE_MIN_RATE else "blur"
        log.info("Clip: cadrage=%s (visage détecté sur %.0f%% du clip)", mode, rate * 100)

        if mode == "blur":
            cmd = [
                settings.FFMPEG_BIN, "-y", "-i", str(seg),
                "-filter_complex", _blur_fit_graph(),
                "-map", "[v]", "-map", "0:a?",
                "-r", str(settings.OUTPUT_FPS),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                str(out_path),
            ]
            _run_ffmpeg(cmd, "cadrage fond flou", cwd=td)
            log.info(
                "Clip: écrit %s (%dx%d)", out_path.name,
                settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT,
            )
            return out_path

        # Plus grand rectangle 9:16 tenant dans la source.
        target_ar = settings.OUTPUT_WIDTH / settings.OUTPUT_HEIGHT  # 9/16
        if w / h > target_ar:  # source paysage (cas YouTube/Twitch) : on rogne en largeur
            crop_h = h - (h % 2)
            crop_w = int(round(h * target_ar))
            crop_w -= crop_w % 2
            track = _smooth_track(samples, w, crop_w)
            crop_y = 0
        else:  # source déjà verticale/carrée : crop centré vertical, pas de tracking X
            crop_w = w - (w % 2)
            crop_h = int(round(w / target_ar))
            crop_h = min(crop_h, h)
            crop_h -= crop_h % 2
            track = None
            crop_y = (h - crop_h) // 2

        scale = f"scale={settings.OUTPUT_WIDTH}:{settings.OUTPUT_HEIGHT}"

        if track:  # recadrage animé (suivi de visage)
            # Fichier de commandes en RELATIF (ffmpeg tourne avec cwd=td) : évite
            # le ':' du chemin Windows qui casserait le parsing du filtergraph.
            _write_sendcmd(track, Path(td) / "cmds.txt")
            x0 = track[0][1]
            vf = (
                f"sendcmd=f=cmds.txt,"
                f"crop=w={crop_w}:h={crop_h}:x={x0}:y={crop_y},"
                f"{scale},setsar=1"
            )
            log.info("Clip: recadrage animé (%d échantillons, visage suivi)", len(track))
        else:  # crop centré statique
            x_center = (w - crop_w) // 2
            x_center -= x_center % 2
            vf = f"crop=w={crop_w}:h={crop_h}:x={x_center}:y={crop_y},{scale},setsar=1"
            log.info("Clip: crop centré statique (pas de visage suivi)")

        cmd = [
            settings.FFMPEG_BIN, "-y", "-i", str(seg),
            "-vf", vf,
            "-r", str(settings.OUTPUT_FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            str(out_path),
        ]
        _run_ffmpeg(cmd, "recadrage 9:16", cwd=td)

    log.info("Clip: écrit %s (%dx%d)", out_path.name, settings.OUTPUT_WIDTH, settings.OUTPUT_HEIGHT)
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
