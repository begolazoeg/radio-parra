"""
Rutas de datos de Radio Parra (§3.3 y §11 de ARCHITECTURE.md).

    data/
      state.db            # SQLite (WAL)
      stock/<kind>/       # audio listo para emitir
      tmp/                # audio a medio generar

Escritura atómica: el audio se genera en ``tmp/`` y se mueve con ``os.replace``
a ``stock/<kind>/``; solo entonces se inserta la fila. ``tmp/`` y ``stock/`` viven
bajo el mismo ``data_dir`` para que el rename sea atómico (mismo sistema de archivos).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

STATE_DB = "state.db"
STOCK_DIRNAME = "stock"
TMP_DIRNAME = "tmp"

_KIND_RE = re.compile(r"[a-z0-9][a-z0-9_]*")


def db_path(data_dir: Path) -> Path:
    """Ruta de la base de datos: ``<data_dir>/state.db``."""
    return data_dir / STATE_DB


def stock_dir(data_dir: Path, kind: str) -> Path:
    """Directorio de stock de un kind: ``<data_dir>/stock/<kind>/`` (no lo crea)."""
    if not _KIND_RE.fullmatch(kind):
        raise ValueError(f"Kind no válido como nombre de directorio: {kind!r}")
    return data_dir / STOCK_DIRNAME / kind


def tmp_dir(data_dir: Path) -> Path:
    """Directorio temporal de generación: ``<data_dir>/tmp/`` (no lo crea)."""
    return data_dir / TMP_DIRNAME


def commit_audio(tmp_path: Path, final_path: Path) -> Path:
    """
    Mueve atómicamente ``tmp_path`` a ``final_path`` (``os.replace``), creando los
    directorios padre si faltan. Devuelve ``final_path``. Si el origen no existe,
    lanza ``FileNotFoundError`` sin tocar el destino.
    """
    if not tmp_path.is_file():
        raise FileNotFoundError(f"No existe el audio temporal: {tmp_path}")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, final_path)
    return final_path
