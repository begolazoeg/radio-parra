"""
Caché en disco de las respuestas de fuentes abiertas (MusicBrainz, Wikipedia,
Wikidata), bajo ``data/cache/sources/``.

- Clave: la URL completa (con query). Archivo: ``<sha256(url)>.json``.
- Se guardan las respuestas 200 (cuerpo JSON) y las 404 (``body=None``), para no
  repetir búsquedas que ya sabemos vacías.
- Caducidad (TTL) por defecto de 30 días; una entrada caducada o ilegible se ignora.
- Escritura atómica (temporal + ``os.replace``). El reloj es inyectable para tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 30 * 24 * 3600


def sources_cache_dir(data_dir: Path) -> Path:
    """Directorio de la caché de fuentes: ``<data_dir>/cache/sources/`` (no lo crea)."""
    return data_dir / "cache" / "sources"


@dataclass(frozen=True)
class CachedResponse:
    """Respuesta guardada: estado HTTP (200 o 404), cuerpo JSON y cuándo se obtuvo."""
    status: int
    body: Any
    fetched_at: float


class SourceCache:
    """Caché JSON en disco con TTL, indexada por URL."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.ttl_s = ttl_s
        self._now = now

    @classmethod
    def for_data_dir(
        cls, data_dir: Path, *, ttl_s: float = DEFAULT_TTL_S, now: Callable[[], float] = time.time
    ) -> SourceCache:
        """Caché en ``<data_dir>/cache/sources/``."""
        return cls(sources_cache_dir(data_dir), ttl_s=ttl_s, now=now)

    def _path(self, url: str) -> Path:
        return self.root / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"

    def get(self, url: str) -> CachedResponse | None:
        """Respuesta guardada para ``url`` o None si no hay, ha caducado o está dañada."""
        path = self._path(url)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = CachedResponse(int(raw["status"]), raw["body"], float(raw["fetched_at"]))
            if raw.get("url") != url:
                return None
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("entrada de caché ilegible, se ignora: %s", path)
            return None
        if self._now() - entry.fetched_at > self.ttl_s:
            return None
        return entry

    def put(self, url: str, status: int, body: Any) -> None:
        """Guarda la respuesta de ``url`` (los fallos de disco se registran y se ignoran)."""
        path = self._path(url)
        payload = {"url": url, "status": status, "fetched_at": self._now(), "body": body}
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.warning("no se pudo escribir la caché de fuentes en %s", path, exc_info=True)
