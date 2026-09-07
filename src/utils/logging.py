"""Logging centralisé.

Un seul point de configuration : `get_logger(name)`. Format lisible en CI
(horodatage + niveau + module), niveau piloté par `settings.LOG_LEVEL`.

Les logs sont écrits sur stderr (visibles dans GitHub Actions). Le Bloc 2
pourra en plus pousser le fichier `pipeline.log` sur Drive ; pour cela on
attache optionnellement un `FileHandler` via `attach_file_handler`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from config import settings

_CONFIGURED = False
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("clipping")
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Retourne un logger enfant du namespace `clipping`."""
    _configure_root()
    return logging.getLogger(f"clipping.{name}")


def attach_file_handler(path: str | Path) -> logging.Handler:
    """Ajoute un handler fichier (pour ensuite uploader le log sur Drive)."""
    _configure_root()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    logging.getLogger("clipping").addHandler(fh)
    return fh
