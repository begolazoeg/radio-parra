"""
Importación de una biblioteca musical local (p. ej. audios de conciertos Tiny Desk).

Recorre un directorio, lee duración y etiquetas con mutagen y registra cada pista
como segmento `kind="music"` en estado `ready`. La importación es idempotente:
las pistas ya registradas (misma ruta absoluta) se omiten.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mutagen

from radio.core.ids import new_id
from radio.core.store import DB

logger = logging.getLogger(__name__)

# Extensiones de audio reconocidas (comparación en minúsculas)
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"})

# Separador "Artista - Título" en nombres de archivo
_ARTIST_TITLE_SEP = " - "


# ── Modelos ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrackMeta:
    """Metadatos mínimos de una pista de audio local."""
    path: Path              # absoluta y resuelta
    title: str
    artist: str | None
    duration_s: float


@dataclass
class ImportReport:
    """Resumen de una importación de directorio."""
    added: int = 0
    skipped_existing: int = 0
    failed: list[Path] = field(default_factory=list)


# ── Utilidades de texto ───────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """
    Convierte un texto a slug ASCII en minúsculas: quita acentos y sustituye
    cualquier secuencia no alfanumérica por "-". Ej.: "Rosalía" → "rosalia".
    """
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def _tidy(text: str) -> str:
    """Colapsa espacios repetidos y recorta extremos."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_stem(stem: str) -> tuple[str | None, str]:
    """
    Deduce (artista, título) a partir del nombre de archivo sin extensión.
    Admite el patrón "Artista - Título" (también con guiones bajos: "Artista_-_Título").
    Sin separador, el título es el nombre con "_" y "-" convertidos en espacios.
    """
    spaced = stem.replace("_", " ")
    if _ARTIST_TITLE_SEP in spaced:
        artist_raw, title_raw = spaced.split(_ARTIST_TITLE_SEP, 1)
        artist, title = _tidy(artist_raw), _tidy(title_raw)
        if artist and title:
            return artist, title
    title = _tidy(spaced.replace("-", " "))
    return None, title or stem


def _first_tag(tags: Any, key: str) -> str | None:
    """Primer valor no vacío de una etiqueta "easy" de mutagen, o None."""
    if tags is None:
        return None
    try:
        values = tags.get(key)
    except Exception:  # algunos formatos no soportan get()
        return None
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    for value in values:
        cleaned = _tidy(str(value))
        if cleaned:
            return cleaned
    return None


# ── Lectura de metadatos ──────────────────────────────────────────────────────

def read_meta(path: Path) -> TrackMeta | None:
    """
    Lee duración y etiquetas (título/artista) de un archivo de audio.
    Si faltan etiquetas, las deduce del nombre de archivo.
    Devuelve None (con aviso en el log) si el archivo no es legible o dura <= 0 s.
    """
    resolved = path.resolve()
    try:
        audio = mutagen.File(resolved, easy=True)
    except Exception as exc:  # mutagen lanza tipos muy variados
        logger.warning("No se pudo leer %s: %s", resolved, exc)
        return None
    if audio is None or getattr(audio, "info", None) is None:
        logger.warning("Formato de audio no reconocido: %s", resolved)
        return None

    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    if duration <= 0:
        logger.warning("Duración no válida (%.3f s) en %s", duration, resolved)
        return None

    stem_artist, stem_title = _parse_stem(resolved.stem)
    title = _first_tag(audio.tags, "title") or stem_title
    artist = _first_tag(audio.tags, "artist") or stem_artist
    return TrackMeta(path=resolved, title=title, artist=artist, duration_s=duration)


# ── Importación ───────────────────────────────────────────────────────────────

def _is_tiny_desk(parts: tuple[str, ...]) -> bool:
    """True si algún componente de la ruta contiene "tiny" y "desk" (sin mayúsculas)."""
    return any("tiny" in p.lower() and "desk" in p.lower() for p in parts)


def _iter_audio_files(root: Path) -> list[Path]:
    """Lista ordenada de archivos de audio bajo root (recursivo, ignora ocultos)."""
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in AUDIO_EXTENSIONS
        and not p.name.startswith(".")
    )


def import_directory(db: DB, root: Path, *, producer: str = "music_library") -> ImportReport:
    """
    Importa recursivamente los audios de root como segmentos musicales listos.
    Etiquetas: "artist:<slug>" si hay artista y "source:tiny_desk" si la ruta
    (relativa a root, más el nombre de root) menciona Tiny Desk.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"No es un directorio: {root}")

    report = ImportReport()
    for file in _iter_audio_files(root):
        resolved = file.resolve()
        if db.get_segment_by_audio_path(resolved) is not None:
            report.skipped_existing += 1
            continue

        meta = read_meta(resolved)
        if meta is None:
            report.failed.append(resolved)
            continue

        tags: list[str] = []
        if meta.artist:
            artist_slug = slugify(meta.artist)
            if artist_slug:
                tags.append(f"artist:{artist_slug}")
        rel_parts = (root.name, *file.relative_to(root).parts)
        if _is_tiny_desk(rel_parts):
            tags.append("source:tiny_desk")

        db.add_segment(
            id=new_id(),
            kind="music",
            status="ready",
            title=meta.title,
            duration_s=meta.duration_s,
            audio_path=meta.path,
            producer=producer,
            source_url=None,
            tags=tags,
        )
        report.added += 1
        logger.info("Importada: %s (%.1f s)", meta.title, meta.duration_s)

    return report
