"""
Resumen de Wikipedia de un artista (API REST ``page/summary``).

Licencia: el texto de Wikipedia es **CC BY-SA 4.0**. La atribución es la URL de la
página, que va en ``SourceDoc.url`` y debe guardarse con el segmento
(``meta["sources"]``). El guion se escribe a partir del resumen (no se copia al aire
tal cual). ``LICENSE`` y ``radio.sources.source_license`` dan la nota de licencia.

Flujo de ``wikipedia_doc``:

- Idiomas en orden: primero el de la emisora (``"es"``), luego ``"en"`` como respaldo.
- Si MusicBrainz ha dado títulos (relaciones ``wikipedia``/``wikidata``), se usan solo
  esos: son la página correcta del artista. Si no hay, se busca
  (``/w/api.php?action=query&list=search``) con una comprobación estricta: el título
  debe ser el nombre del artista, o el nombre con una aclaración musical entre
  paréntesis ("Nube Ferrán (cantante)"), y el resumen debe mencionar al artista.
- Se rechazan páginas de desambiguación (``type == "disambiguation"``) y cualquier
  ``type`` distinto de ``"standard"``, y los resúmenes vacíos.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from radio.core.models import SourceDoc
from radio.grounding.lexicon import fold
from radio.sources.http import Fetcher
from radio.sources.jsonutil import get_dict, get_list, get_str
from radio.sources.musicbrainz import normalize_name

LICENSE = "CC BY-SA 4.0"
MAX_EXTRACT_CHARS = 3000
SEARCH_LIMIT = 5

# Aclaraciones entre paréntesis que indican que la página es de un músico/grupo
MUSIC_QUALIFIERS = frozenset({
    "band", "banda", "grupo", "group", "musician", "musico", "musica", "singer",
    "cantante", "cantautor", "cantautora", "singer-songwriter", "rapper", "rapero",
    "rapera", "dj", "composer", "compositor", "compositora", "duo", "trio", "orquesta",
    "orchestra", "artist", "artista", "guitarist", "guitarrista", "pianist", "pianista",
})
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]+)?$")
_QUALIFIED_RE = re.compile(r"^(?P<base>.+?)\s*\((?P<qual>[^()]+)\)$")


def _api_root(lang: str) -> str:
    if not _LANG_RE.match(lang):
        raise ValueError(f"idioma de Wikipedia no válido: {lang!r}")
    return f"https://{lang}.wikipedia.org"


def page_url(lang: str, title: str) -> str:
    """URL pública de una página (``https://es.wikipedia.org/wiki/Título``)."""
    return f"{_api_root(lang)}/wiki/{quote(title.replace(' ', '_'), safe='')}"


def is_acceptable_title(title: str, artist: str) -> bool:
    """
    ¿Es ``title`` la página del artista? Igual al nombre (normalizado) o nombre +
    aclaración musical entre paréntesis. Cualquier otra cosa se rechaza.
    """
    if normalize_name(title) == normalize_name(artist):
        return True
    match = _QUALIFIED_RE.match(title.strip())
    if match is None or normalize_name(match["base"]) != normalize_name(artist):
        return False
    qualifier_words = set(re.split(r"[\s,]+", fold(match["qual"])))
    return bool(qualifier_words & MUSIC_QUALIFIERS)


def fetch_summary(fetcher: Fetcher, lang: str, title: str) -> dict[str, Any] | None:
    """JSON de ``/api/rest_v1/page/summary/<título>`` o None si no existe."""
    url = f"{_api_root(lang)}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}"
    payload = fetcher.get_json(url)
    return payload if isinstance(payload, dict) else None


def search_title(fetcher: Fetcher, lang: str, artist: str) -> str | None:
    """Primer resultado de búsqueda cuyo título pasa ``is_acceptable_title``."""
    payload = fetcher.get_json(
        f"{_api_root(lang)}/w/api.php",
        {"action": "query", "list": "search", "srsearch": artist, "format": "json",
         "srlimit": str(SEARCH_LIMIT)},
    )
    for hit in get_list(get_dict(payload, "query"), "search"):
        title = get_str(hit, "title")
        if title and is_acceptable_title(title, artist):
            return title
    return None


def summary_doc(data: Mapping[str, Any], lang: str) -> SourceDoc | None:
    """``SourceDoc`` de un resumen, o None si es desambiguación, no estándar o vacío."""
    if get_str(data, "type") != "standard":
        return None
    extract = get_str(data, "extract")
    title = get_str(get_dict(data, "titles"), "canonical") or get_str(data, "title").replace(" ", "_")
    if not extract or not title:
        return None
    url = get_str(get_dict(get_dict(data, "content_urls"), "desktop"), "page") or page_url(lang, title)
    return SourceDoc(id=f"wp:{lang}:{title}", text=extract[:MAX_EXTRACT_CHARS], url=url)


def _mentions(text: str, artist: str) -> bool:
    return normalize_name(artist) in normalize_name(text)


def wikipedia_doc(
    fetcher: Fetcher,
    artist: str,
    *,
    langs: Sequence[str] = ("es", "en"),
    titles: Mapping[str, str] | None = None,
) -> SourceDoc | None:
    """
    Resumen de Wikipedia del artista en el primer idioma de ``langs`` que lo tenga.
    ``titles`` (de MusicBrainz) evita la búsqueda. Lanza ``SourceError`` si falla la red.
    """
    for lang in dict.fromkeys(langs):
        if titles:
            title = titles.get(lang)
            trusted = True
        else:
            title = search_title(fetcher, lang, artist)
            trusted = False
        if not title:
            continue
        data = fetch_summary(fetcher, lang, title)
        if data is None:
            continue
        doc = summary_doc(data, lang)
        if doc is None:
            continue
        if not trusted and not _mentions(doc.text, artist):
            continue
        return doc
    return None
