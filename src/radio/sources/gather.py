"""
``gather_artist_sources``: la etapa *gather* de ``host_intro`` (§4.2 paso 1, §12 Fase 2).

Decisión de la dueña (2026-09-24): los datos de una intro vienen **solo** de fuentes
abiertas — MusicBrainz (datos centrales CC0) y Wikipedia (CC BY-SA 4.0) — más el
título del episodio como identificador. La descripción del episodio de NPR **nunca**
es una fuente (sus términos prohíben usar su contenido para construir sistemas de IA),
así que este módulo ni la recibe.

Devuelve 0–2 ``SourceDoc`` con ids estables:

- ``"mb:<mbid>"``: ficha de MusicBrainz (``radio.sources.musicbrainz``).
- ``"wp:<lang>:<Título_canónico>"``: resumen de Wikipedia en el idioma de la emisora
  o, si no hay, en inglés (``radio.sources.wikipedia``).

Nunca lanza por fallos de red, HTTP o datos raros: se registran y la lista sale más
corta (o vacía), y el productor hace la versión sin dato (invariante 5). Un artista
ambiguo en MusicBrainz devuelve ``[]`` sin consultar Wikipedia.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from radio.core.models import SourceDoc
from radio.sources.cache import SourceCache
from radio.sources.http import Fetcher, RateLimiter, SourceError
from radio.sources.musicbrainz import (
    MatchStatus,
    artist_doc,
    lookup_artist,
    search_artist,
    wikipedia_titles,
)
from radio.sources.wikipedia import wikipedia_doc

logger = logging.getLogger(__name__)

# Fallos de fuente o de forma de datos que degradan a "sin esa fuente"
_SOFT_ERRORS = (SourceError, KeyError, TypeError, ValueError)


def gather_artist_sources(
    artist: str,
    *,
    client: httpx.Client,
    cache: SourceCache | None = None,
    lang: str = "es",
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    limiter: RateLimiter | None = None,
) -> list[SourceDoc]:
    """
    Fuentes abiertas sobre ``artist`` (nombre tal cual, p. ej. de
    ``radio.music.feed.artist_from_title``).

    - ``client``: ``httpx.Client`` (en tests, con ``httpx.MockTransport``).
    - ``cache``: caché en disco (``SourceCache.for_data_dir(data_dir)``) o None.
    - ``lang``: idioma de la emisora; Wikipedia se prueba en ``lang`` y luego en ``en``.
    - ``clock``/``sleep``: reloj monótono y espera, para el límite de tasa.
    - ``limiter``: comparte el límite de tasa entre llamadas de una misma ejecución
      (si se pasa, ``clock`` y ``sleep`` se ignoran).
    """
    artist = artist.strip()
    if not artist:
        return []
    fetcher = Fetcher(client, cache=cache, limiter=limiter or RateLimiter(clock=clock, sleep=sleep))
    docs: list[SourceDoc] = []
    titles: dict[str, str] = {}

    try:
        match = search_artist(fetcher, artist)
    except _SOFT_ERRORS as exc:
        logger.warning("MusicBrainz no disponible para %r: %s", artist, exc)
        match = None
    if match is not None and match.status is MatchStatus.AMBIGUOUS:
        logger.info("artista ambiguo en MusicBrainz, sin fuentes: %r", artist)
        return []
    if match is not None and match.mbid is not None:
        try:
            data = lookup_artist(fetcher, match.mbid)
            if data is not None:
                doc = artist_doc(data)
                if doc is not None:
                    docs.append(doc)
                titles = wikipedia_titles(fetcher, data)
        except _SOFT_ERRORS as exc:
            logger.warning("ficha de MusicBrainz incompleta para %r: %s", artist, exc)

    try:
        wp = wikipedia_doc(fetcher, artist, langs=(lang, "en"), titles=titles)
    except _SOFT_ERRORS as exc:
        logger.warning("Wikipedia no disponible para %r: %s", artist, exc)
        wp = None
    if wp is not None:
        docs.append(wp)
    return docs
