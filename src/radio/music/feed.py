"""
Lectura del feed RSS oficial de música (§7: la música solo entra por el feed RSS
oficial del podcast; nada de scraping).

- ``fetch_feed``: GET del feed con ``User-Agent`` propio, tiempos límite y GET
  condicional (``ETag`` / ``Last-Modified``). Si el servidor responde 304 se reutiliza
  el cuerpo guardado.
- ``FeedCache``: persistencia mínima del GET condicional y de los episodios
  descartados, como archivos en ``data/cache/feeds/`` (§11). Es caché regenerable:
  no merece tabla propia ni cambio de esquema, y ``universe_state`` es para ficción.
- ``parse_entries``: entradas con enclosure de **audio** (los de vídeo se ignoran),
  de la más reciente a la más antigua.
- ``clean_text`` / ``artist_from_title``: título en texto plano y artista deducido de
  "Artista: Tiny Desk…".

La descripción de los episodios **no se lee**: la dueña decidió que las descripciones
de NPR nunca se usen como fuente de un LLM (términos de NPR sobre sistemas de IA).

La URL del feed es la decisión abierta #8: no hay ninguna por defecto en el código.
"""

from __future__ import annotations

import calendar
import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import feedparser
import httpx

from radio.music.library import AUDIO_EXTENSIONS

USER_AGENT = "RadioParra/0.1 (+contacto en README)"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_FEED_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 4000

# Tipo MIME → extensión, para enclosures cuya URL no la trae
_MIME_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}

# "Artista: Tiny Desk Concert", "Artista - Tiny Desk (Home) Concert"...
_ARTIST_RE = re.compile(r"^\s*(?P<artist>.+?)\s*(?::|\s[-–—])\s*tiny desk\b", re.IGNORECASE)


class FeedError(RuntimeError):
    """El feed no se ha podido descargar o no es un RSS válido."""


# ── Texto ─────────────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")


def clean_text(raw: str, limit: int = MAX_TEXT_CHARS) -> str:
    """HTML → texto plano: sin etiquetas, entidades resueltas, espacios colapsados."""
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    text = re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()
    return text[:limit].rstrip()


def artist_from_title(title: str) -> str | None:
    """Artista de un título tipo "Artista: Tiny Desk Concert" (None si no encaja)."""
    match = _ARTIST_RE.match(title)
    if match is None:
        return None
    artist = match.group("artist").strip(" \"'")
    return artist or None


# ── Entradas ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeedEntry:
    """Un episodio del feed con su enclosure de audio."""
    guid: str
    title: str
    audio_url: str
    mime: str
    length: int | None
    published: datetime | None
    link: str

    @property
    def ext(self) -> str:
        """Extensión del audio (de la URL; si no, del tipo MIME; por defecto .mp3)."""
        suffix = Path(httpx.URL(self.audio_url).path).suffix.lower()
        if suffix in AUDIO_EXTENSIONS:
            return suffix
        return _MIME_EXT.get(self.mime.split(";")[0].strip().lower(), ".mp3")


def _audio_enclosure(entry: Any) -> tuple[str, str, int | None] | None:
    """(url, mime, tamaño) del primer enclosure de audio, o None."""
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        if not href:
            continue
        mime = str(enc.get("type") or "").lower()
        suffix = Path(httpx.URL(href).path).suffix.lower()
        if mime.startswith("audio/") or (not mime and suffix in AUDIO_EXTENSIONS):
            try:
                length = int(enc.get("length") or 0) or None
            except (TypeError, ValueError):
                length = None
            return str(href), mime, length
    return None


def _published(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), UTC)


def parse_entries(body: bytes) -> list[FeedEntry]:
    """
    Entradas con enclosure de audio, de la más reciente a la más antigua (las que no
    tienen fecha, al final y en el orden del feed). FeedError si no es un feed.
    """
    parsed = feedparser.parse(body)
    if parsed.get("bozo") and not parsed.get("entries") and not parsed.get("feed"):
        raise FeedError(f"feed no válido: {parsed.get('bozo_exception')}")
    entries: list[FeedEntry] = []
    for entry in parsed.get("entries", []):
        enclosure = _audio_enclosure(entry)
        if enclosure is None:
            continue
        url, mime, length = enclosure
        guid = str(entry.get("id") or url)
        entries.append(FeedEntry(
            guid=guid,
            title=clean_text(str(entry.get("title") or ""), 300) or guid,
            audio_url=url,
            mime=mime,
            length=length,
            published=_published(entry),
            link=str(entry.get("link") or ""),
        ))
    oldest = datetime.min.replace(tzinfo=UTC)
    # sort estable: sin fecha → al final, conservando el orden del feed
    return sorted(entries, key=lambda e: (e.published is not None, e.published or oldest),
                  reverse=True)


# ── Caché del feed ────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@dataclass
class FeedCache:
    """
    Estado del GET condicional y episodios descartados de un productor:
    ``<dir>/<name>.json`` (metadatos) y ``<dir>/<name>.xml`` (último cuerpo).
    Si cambia la URL del feed, se empieza de cero.
    """
    directory: Path
    name: str
    url: str = ""
    etag: str | None = None
    last_modified: str | None = None
    rejected: dict[str, str] = field(default_factory=dict)   # guid → motivo

    @property
    def meta_path(self) -> Path:
        return self.directory / f"{self.name}.json"

    @property
    def body_path(self) -> Path:
        return self.directory / f"{self.name}.xml"

    @classmethod
    def load(cls, directory: Path, name: str, url: str) -> FeedCache:
        cache = cls(directory, name, url)
        try:
            data = json.loads(cache.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cache
        if not isinstance(data, dict) or data.get("url") != url:
            return cache
        cache.etag = data.get("etag")
        cache.last_modified = data.get("last_modified")
        rejected = data.get("rejected")
        cache.rejected = dict(rejected) if isinstance(rejected, dict) else {}
        return cache

    def save(self) -> None:
        data = {
            "url": self.url,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "rejected": self.rejected,
        }
        _atomic_write(
            self.meta_path,
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )

    def cached_body(self) -> bytes | None:
        try:
            return self.body_path.read_bytes()
        except OSError:
            return None

    def store(self, body: bytes, etag: str | None, last_modified: str | None) -> None:
        _atomic_write(self.body_path, body)
        self.etag = etag
        self.last_modified = last_modified
        self.save()


@dataclass(frozen=True)
class FeedResponse:
    """Cuerpo del feed y validadores HTTP (``not_modified``: vino de la caché por 304)."""
    body: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


def fetch_feed(client: httpx.Client, url: str, cache: FeedCache | None = None) -> FeedResponse:
    """
    Descarga el feed (GET condicional si hay caché con cuerpo). No guarda nada: el
    llamador llama a ``cache.store`` solo cuando el cuerpo se ha podido interpretar.
    Errores de red: ``httpx.HTTPError``; respuesta inesperada: ``FeedError``.
    """
    headers = {"User-Agent": USER_AGENT}
    cached = cache.cached_body() if cache is not None else None
    if cache is not None and cached is not None:
        if cache.etag:
            headers["If-None-Match"] = cache.etag
        if cache.last_modified:
            headers["If-Modified-Since"] = cache.last_modified
    resp = client.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
    if resp.status_code == 304 and cache is not None and cached is not None:
        return FeedResponse(cached, cache.etag, cache.last_modified, not_modified=True)
    if resp.status_code != 200:
        raise FeedError(f"el feed ha respondido HTTP {resp.status_code}")
    body = resp.content
    if len(body) > MAX_FEED_BYTES:
        raise FeedError(f"feed demasiado grande ({len(body)} bytes)")
    return FeedResponse(body, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
