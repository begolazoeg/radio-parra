"""
Dobles de ``host_intro`` para ``radio simulate`` y ``radio preview host_intro --fake``
(sin red, sin claves, deterministas).

- ``fake_sources(artist, lang)``: una ficha sintética con la forma de MusicBrainz
  (``mb:fake-<slug>``), con un lugar y un año inventados para una artista ficticia.
  No describe a nadie real: los artistas de la simulación son sintéticos y la
  previsualización usa ``FAKE_ARTIST`` ("Nube Ferrán", la artista ficticia de los
  tests de fuentes).
- ``fake_intro_responder(system, user)``: "LLM" que contesta con un guion **bien
  pegado** a esa ficha (claims que citan su id) o, si el mensaje no trae fuentes, con
  una intro sin datos. Lee el artista y los ids del propio mensaje (formato de
  ``prompts/factual/host_intro_user.j2``): es un doble para probar el pipeline, no
  un modelo.
- ``fake_intro_llm()``: ``FakeLLM`` con ese respondedor.
"""

from __future__ import annotations

import re
from typing import Any

from radio.core.models import SourceDoc
from radio.music.library import slugify
from radio.providers.llm.fake import FakeLLM

FAKE_ARTIST = "Nube Ferrán"
FAKE_TITLE = f"{FAKE_ARTIST}: Tiny Desk Concert"
FAKE_PLACE = "Villa Parral"
FAKE_YEAR = "2011"

_ARTIST_RE = re.compile(r"^- Artista: (?P<artist>.+)$", re.MULTILINE)
_SOURCE_RE = re.compile(r'<fuente id="(?P<id>[^"]+)">')


def fake_sources(artist: str, lang: str = "es") -> list[SourceDoc]:
    """Ficha sintética (sin red) para ``artist``."""
    slug = slugify(artist) or "artista"
    return [SourceDoc(
        id=f"mb:fake-{slug}",
        text=(f"Nombre: {artist}\nTipo: grupo (Group)\n"
              f"Zona de origen: {FAKE_PLACE}\nFormación: {FAKE_YEAR}"),
        url=f"https://musicbrainz.org/artist/fake-{slug}",
    )]


def fake_intro_responder(system: str, user: str) -> dict[str, Any]:
    """Respuesta JSON de intro: con datos si hay fuentes en ``user``; si no, sin datos."""
    m = _ARTIST_RE.search(user)
    artist = m.group("artist").strip() if m else ""
    if not artist or artist.startswith("("):
        artist = ""
    ids = [i for i in _SOURCE_RE.findall(user) if i != "..."]
    if ids and artist:
        first = f"Desde {FAKE_PLACE} llega {artist}"
        second = f"un grupo en activo desde {FAKE_YEAR}"
        return {
            "script": (f"{first}, {second}. Ahora suena su concierto del Tiny Desk, "
                       "íntimo y sin prisas."),
            "claims": [
                {"text": first, "source_id": ids[0]},
                {"text": second, "source_id": ids[0]},
            ],
        }
    who = artist or "un concierto muy especial"
    return {
        "script": (f"Y ahora, desde el Tiny Desk, llega {who}. "
                   "Un directo íntimo para escuchar con calma."),
        "claims": [],
    }


def fake_intro_llm(cost_eur: float = 0.0) -> FakeLLM:
    """``FakeLLM`` que responde con ``fake_intro_responder``."""
    return FakeLLM(responder=fake_intro_responder, cost_eur=cost_eur)
