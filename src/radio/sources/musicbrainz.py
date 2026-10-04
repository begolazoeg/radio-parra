"""
Ficha de artista desde MusicBrainz (Web Service v2, JSON).

Licencia: los **datos centrales** de MusicBrainz (artistas, áreas, fechas,
relaciones de URL) son **CC0** (dominio público). Por eso la ficha solo usa datos
centrales: tipo, país, área, zona de origen, fechas de ``life-span`` y aclaración.
Las etiquetas/géneros y anotaciones son *datos suplementarios* con licencia
CC BY-NC-SA, así que no se piden ni se usan.

Política de uso: ``User-Agent`` descriptivo y como máximo 1 petición por segundo
(lo garantizan ``radio.sources.http.Fetcher`` y ``RateLimiter``); un 503 indica
exceso de tasa y se reintenta con espera.

Flujo:

1. ``search_artist``: ``/ws/2/artist/?query=artist:"<nombre>"&fmt=json``. Candidatos
   con ``score >= MIN_SCORE`` y nombre equivalente (sin tildes/mayúsculas ni "The"
   inicial, o parecido ≥ ``MIN_NAME_SIMILARITY``). Ninguno → ``NOT_FOUND``; más de
   uno → ``AMBIGUOUS`` (mejor sin fuente que con la de otro artista).
2. ``lookup_artist``: ``/ws/2/artist/<mbid>?inc=url-rels&fmt=json``.
3. ``artist_doc``: ``SourceDoc(id="mb:<mbid>", text=ficha, url=página del artista)``.
4. ``wikipedia_titles``: títulos de Wikipedia por idioma a partir de las relaciones
   ``wikipedia`` y ``wikidata`` (sitelinks de Wikidata, también CC0).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import unquote, urlsplit

from radio.core.models import SourceDoc
from radio.grounding.lexicon import fold
from radio.sources.countries import country_names, spanish_name_for
from radio.sources.http import Fetcher
from radio.sources.jsonutil import get_dict, get_list, get_str

API_ROOT = "https://musicbrainz.org/ws/2"
SITE_ROOT = "https://musicbrainz.org"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
LICENSE = "CC0 1.0 (datos centrales de MusicBrainz)"

MIN_SCORE = 90
MIN_NAME_SIMILARITY = 0.92
SEARCH_LIMIT = 5

_TYPES_ES = {
    "Person": "persona", "Group": "grupo", "Orchestra": "orquesta", "Choir": "coro",
    "Character": "personaje", "Other": "otro",
}
_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_QID_RE = re.compile(r"^Q\d+$")


class MatchStatus(Enum):
    """Resultado de la búsqueda de un artista."""
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ArtistMatch:
    """Resultado de ``search_artist`` (``mbid`` solo si ``FOUND``)."""
    status: MatchStatus
    mbid: str | None = None
    name: str | None = None


def normalize_name(name: str) -> str:
    """Nombre comparable: sin tildes, minúsculas, "&"→"and", sin "the" inicial ni signos."""
    folded = fold(name).replace("&", " and ")
    folded = re.sub(r"[^\w\s]", " ", folded)
    words = folded.split()
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(words)


def names_match(a: str, b: str) -> bool:
    """¿Son ``a`` y ``b`` el mismo nombre de artista (salvo detalles de escritura)?"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= MIN_NAME_SIMILARITY


def _escape_lucene(text: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", text)


def select_match(payload: Any, artist: str) -> ArtistMatch:
    """Elige el artista de una respuesta de búsqueda (función pura, ver módulo)."""
    candidates: list[tuple[str, str]] = []
    for item in get_list(payload, "artists"):
        mbid, name = get_str(item, "id"), get_str(item, "name")
        try:
            score = int(item.get("score", 0)) if isinstance(item, dict) else 0
        except (TypeError, ValueError):
            score = 0
        if score >= MIN_SCORE and _MBID_RE.match(mbid) and names_match(name, artist):
            candidates.append((mbid, name))
    if not candidates:
        return ArtistMatch(MatchStatus.NOT_FOUND)
    if len({mbid for mbid, _ in candidates}) > 1:
        return ArtistMatch(MatchStatus.AMBIGUOUS)
    mbid, name = candidates[0]
    return ArtistMatch(MatchStatus.FOUND, mbid, name)


def search_artist(fetcher: Fetcher, artist: str) -> ArtistMatch:
    """Busca ``artist`` en MusicBrainz. Lanza ``SourceError`` si falla la red."""
    payload = fetcher.get_json(
        f"{API_ROOT}/artist/",
        {"query": f'artist:"{_escape_lucene(artist)}"', "fmt": "json",
         "limit": str(SEARCH_LIMIT)},
    )
    return select_match(payload, artist)


def lookup_artist(fetcher: Fetcher, mbid: str) -> dict[str, Any] | None:
    """Ficha completa del artista (con relaciones de URL) o None si no existe."""
    if not _MBID_RE.match(mbid):
        raise ValueError(f"MBID no válido: {mbid!r}")
    payload = fetcher.get_json(f"{API_ROOT}/artist/{mbid}", {"inc": "url-rels", "fmt": "json"})
    return payload if isinstance(payload, dict) else None


def artist_url(mbid: str) -> str:
    """Página pública del artista (atribución)."""
    return f"{SITE_ROOT}/artist/{mbid}"


def artist_text(data: dict[str, Any]) -> str:
    """
    Ficha compacta en español con los datos centrales. Las fechas van en ISO
    (el grounding las entiende) y el país en español, inglés y alias.
    """
    kind = get_str(data, "type")
    lines = [f"Nombre: {get_str(data, 'name')}"]
    if kind:
        lines.append(f"Tipo: {_TYPES_ES.get(kind, kind)} ({kind})")
    country = get_str(data, "country")
    if country:
        lines.append(f"País: {', '.join(country_names(country))}")
    area = get_str(get_dict(data, "area"), "name")
    if area and area not in country_names(country):
        es = spanish_name_for(area)
        lines.append(f"Área: {area}" + (f" ({es})" if es and es != area else ""))
    origin = get_str(get_dict(data, "begin-area"), "name")
    if origin:
        es = spanish_name_for(origin)
        lines.append(f"Zona de origen: {origin}" + (f" ({es})" if es and es != origin else ""))
    span = get_dict(data, "life-span")
    begin, end = get_str(span, "begin"), get_str(span, "end")
    begin_label, end_label = {
        "Person": ("Nacimiento", "Fallecimiento"),
        "Group": ("Formación", "Disolución"),
    }.get(kind, ("Inicio", "Fin"))
    if begin:
        lines.append(f"{begin_label}: {begin}")
    if end:
        lines.append(f"{end_label}: {end}")
    disambiguation = get_str(data, "disambiguation")
    if disambiguation:
        lines.append(f"Aclaración: {disambiguation}")
    return "\n".join(lines)


def artist_doc(data: dict[str, Any]) -> SourceDoc | None:
    """``SourceDoc`` de la ficha, o None si faltan id o nombre."""
    mbid, name = get_str(data, "id"), get_str(data, "name")
    if not _MBID_RE.match(mbid) or not name:
        return None
    return SourceDoc(id=f"mb:{mbid}", text=artist_text(data), url=artist_url(mbid))


def _relation_urls(data: dict[str, Any], rel_type: str) -> list[str]:
    urls = []
    for rel in get_list(data, "relations"):
        if get_str(rel, "type") == rel_type:
            resource = get_str(get_dict(rel, "url"), "resource")
            if resource:
                urls.append(resource)
    return urls


def _wikipedia_title_from_url(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host.endswith(".wikipedia.org") or not parts.path.startswith("/wiki/"):
        return None
    lang = host.split(".")[0]
    title = unquote(parts.path[len("/wiki/"):]).replace("_", " ").strip()
    return (lang, title) if title else None


def wikipedia_titles(fetcher: Fetcher, data: dict[str, Any]) -> dict[str, str]:
    """
    Títulos de Wikipedia por idioma (``{"es": "...", "en": "..."}``) según las
    relaciones del artista: enlace directo a Wikipedia y sitelinks de Wikidata.
    Lanza ``SourceError`` si falla la consulta a Wikidata.
    """
    titles: dict[str, str] = {}
    for url in _relation_urls(data, "wikipedia"):
        found = _wikipedia_title_from_url(url)
        if found:
            titles.setdefault(*found)
    for url in _relation_urls(data, "wikidata"):
        qid = url.rstrip("/").rsplit("/", 1)[-1]
        if not _QID_RE.match(qid):
            continue
        payload = fetcher.get_json(
            WIKIDATA_API,
            {"action": "wbgetentities", "ids": qid, "props": "sitelinks", "format": "json"},
        )
        sitelinks = get_dict(get_dict(get_dict(payload, "entities"), qid), "sitelinks")
        for site, link in sitelinks.items():
            if site.endswith("wiki") and site not in ("commonswiki", "specieswiki"):
                title = get_str(link, "title")
                if title:
                    titles.setdefault(site[: -len("wiki")].replace("_", "-"), title)
        break   # una entidad basta
    return titles
