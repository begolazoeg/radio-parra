"""
Fuentes abiertas para segmentos factuales (§4.2 paso 1 *gather*, §12 Fase 2).

Solo fuentes abiertas (decisión de la dueña, 2026-09-24):

| Fuente | Id de ``SourceDoc`` | Licencia | Atribución |
|---|---|---|---|
| MusicBrainz (datos centrales) | ``mb:<mbid>`` | CC0 | no obligatoria; ``url`` = página del artista |
| Wikipedia (resumen) | ``wp:<lang>:<Título>`` | CC BY-SA 4.0 | obligatoria: ``url`` = página, guardada en ``meta["sources"]`` |

La descripción del episodio de NPR **no** es una fuente (sus términos prohíben usar
su contenido para construir sistemas de IA); el título del episodio solo se usa como
identificador (``allowed_terms`` del grounding).

API principal: ``gather_artist_sources(artist, client=..., cache=...)``. Todas las
peticiones llevan ``User-Agent`` descriptivo, respetan el límite de tasa por host
(MusicBrainz ≤ 1/s) y pasan por la caché en disco si se da.
"""

from __future__ import annotations

from radio.core.models import SourceDoc
from radio.sources.cache import (
    DEFAULT_TTL_S,
    CachedResponse,
    SourceCache,
    sources_cache_dir,
)
from radio.sources.gather import gather_artist_sources
from radio.sources.http import Fetcher, RateLimiter, SourceError
from radio.sources.musicbrainz import LICENSE as MUSICBRAINZ_LICENSE
from radio.sources.wikipedia import LICENSE as WIKIPEDIA_LICENSE


def source_license(doc: SourceDoc) -> str | None:
    """Nota de licencia según el prefijo del id (``mb:`` / ``wp:``); None si es otro."""
    if doc.id.startswith("mb:"):
        return MUSICBRAINZ_LICENSE
    if doc.id.startswith("wp:"):
        return WIKIPEDIA_LICENSE
    return None


def source_meta(doc: SourceDoc) -> dict[str, str | None]:
    """Entrada para ``Segment.meta["sources"]``: id, url, texto y licencia."""
    return {"id": doc.id, "url": doc.url, "text": doc.text, "license": source_license(doc)}


__all__ = [
    "DEFAULT_TTL_S",
    "MUSICBRAINZ_LICENSE",
    "WIKIPEDIA_LICENSE",
    "CachedResponse",
    "Fetcher",
    "RateLimiter",
    "SourceCache",
    "SourceError",
    "gather_artist_sources",
    "source_license",
    "source_meta",
    "sources_cache_dir",
]
