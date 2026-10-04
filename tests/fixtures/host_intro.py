"""
Ayudas para los tests de ``host_intro`` (Fase 2), sin red:

- ``SourcesMock``: transporte ``httpx.MockTransport`` que sirve las respuestas
  sintéticas de ``tests/fixtures/sources/`` (artista ficticia "Nube Ferrán") y
  registra las peticiones.
- Respuestas del LLM para esa artista: ``GOOD`` (pegada a las fuentes),
  ``INVENTED`` (con un dato que no está en ninguna fuente), ``FACT_FREE``.
- ``add_music`` y ``make_ctx``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from radio.core.clock import FakeClock
from radio.core.config import RadioConfig
from radio.core.models import Segment
from radio.core.store import DB
from radio.producers import ProducerContext
from radio.providers.llm.base import LLM
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.base import TTS
from radio.providers.tts.fake import FakeTTS

REPO = Path(__file__).parents[2]
SOURCES = Path(__file__).with_name("sources")
MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=MADRID)

ARTIST = "Nube Ferrán"
TITLE = "Nube Ferrán: Tiny Desk Concert"
MBID = "5e1f0a3c-0000-4000-8000-00000000f001"
MB_ID = f"mb:{MBID}"
WP_ID = "wp:es:Nube_Ferrán"

# Texto del episodio de NPR que NUNCA debe llegar al LLM (ADR 0003)
NPR_DESCRIPTION = "SENTINEL-NPR-DESCRIPTION: texto de la descripcion del episodio"
NPR_LINK = "https://www.npr.org/sentinel-episode-link"
NPR_ENCLOSURE = "https://media.npr.invalid/sentinel-enclosure.mp3"

ROUTES: dict[str, str] = {
    "musicbrainz.org/ws/2/artist/": "mb_search_nube.json",
    f"musicbrainz.org/ws/2/artist/{MBID}": "mb_artist_nube.json",
    "www.wikidata.org/w/api.php": "wd_entities_nube.json",
    "es.wikipedia.org/api/rest_v1/page/summary/Nube_Ferrán": "wp_es_summary_nube.json",
    "en.wikipedia.org/api/rest_v1/page/summary/Nube_Ferrán_(singer)": "wp_en_summary_nube.json",
}

GOOD: dict[str, Any] = {
    "script": ("Desde Villa Parral llega Nube Ferrán, que ha publicado tres álbumes de "
               "estudio. Ahora suena su concierto del Tiny Desk, aquí en Radio Parra."),
    "claims": [
        {"text": "Desde Villa Parral llega Nube Ferrán", "source_id": MB_ID},
        {"text": "ha publicado tres álbumes de estudio", "source_id": WP_ID},
    ],
}
# "dos premios Grammy" no está en ninguna fuente
INVENTED: dict[str, Any] = {
    "script": ("Desde Villa Parral llega Nube Ferrán, que ganó dos premios Grammy. "
               "Ahora suena su concierto del Tiny Desk."),
    "claims": [
        {"text": "Desde Villa Parral llega Nube Ferrán", "source_id": MB_ID},
        {"text": "ganó dos premios Grammy", "source_id": WP_ID},
    ],
}
FACT_FREE: dict[str, Any] = {
    "script": ("Y ahora, desde el Tiny Desk, llega Nube Ferrán. Un directo íntimo para "
               "escuchar con calma aquí, en Radio Parra."),
    "claims": [],
}
# Sin datos... pero con un nombre propio inventado: la versión sin dato tampoco vale
FAKE_FACT_FREE: dict[str, Any] = {
    "script": "Y ahora llega Nube Ferrán con su banda de Toronto. Un directo del Tiny Desk.",
    "claims": [],
}


class SourcesMock:
    """Enruta por ``host + path`` a los fixtures; 404 para lo demás."""

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes = dict(ROUTES if routes is None else routes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = self.routes.get(f"{request.url.host}{request.url.path}")
        if name is None:
            return httpx.Response(404)
        data = json.loads((SOURCES / name).read_text(encoding="utf-8"))
        return httpx.Response(200, json=data)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class FakeTime:
    """Reloj monótono y ``sleep`` falsos para el límite de tasa."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def add_music(
    db: DB, tmp_path: Path, seg_id: str, title: str = TITLE, *,
    minute: int = 0, **meta: Any,
) -> Segment:
    """Canción ``ready`` con audio en disco y ``meta`` como la de ``music_tinydesk``."""
    path = tmp_path / "stock" / "music" / f"{seg_id}.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    seg = Segment(
        id=seg_id, kind="music", factual=False, path=path, duration_s=900.0,
        created_at=NOW.replace(minute=minute), producer="music_tinydesk",
        meta={"title": title, "tags": ["source:tiny_desk"], **meta},
    )
    db.add_segment(seg)
    return seg


def make_ctx(
    tmp_path: Path,
    llm: LLM | None = None,
    *,
    tts: TTS | None = None,
    config: RadioConfig | None = None,
    db: DB | None = None,
    now: datetime = NOW,
) -> ProducerContext:
    return ProducerContext(
        db=db or DB(":memory:"),
        clock=FakeClock(now),
        llm=llm or FakeLLM(GOOD),
        tts=tts or FakeTTS(),
        config=config or RadioConfig.load(REPO / "config"),
        data_dir=tmp_path,
        prompts_dir=REPO / "prompts",
    )
