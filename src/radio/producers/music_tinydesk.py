"""
Productor ``music_tinydesk`` (Fase 1, §12): feed RSS oficial → caché → ``Segment``.

Música solo por el feed RSS oficial del podcast (§7, invariante 9). La URL es la
decisión abierta #8: se lee de ``producers.yaml → music_tinydesk.params.feed_url`` y
**no hay valor por defecto**; sin ella la ejecución falla con un error claro.

Pipeline (sobre ``StagedProducer``):

- ``gather``: GET condicional del feed (``music.feed``), entradas con enclosure de
  audio, sin las ya conocidas (``guid`` en cualquier estado) ni las descartadas
  antes, de la más reciente a la más antigua. Cuántas: el déficit, pero al menos
  ``rotate_per_run`` para que entren episodios nuevos aunque el stock esté lleno, y
  como mucho ``max_per_run`` (descargas grandes: el resto, en la siguiente pasada).
- ``write`` / ``validate``: no hay guion (no hay locución).
- ``tts``: en música esta etapa **descarga** el enclosure en ``data/tmp/`` (en
  streaming, secuencial, con pausa ``download_delay_s`` entre descargas) y comprueba
  código HTTP, tipo de contenido, tamaño y duración (mutagen, > 0). Un episodio
  defectuoso (4xx, no audio, vacío…) se descarta y se recuerda para no reintentarlo;
  un error de red o 5xx hace fallar la ejecución (se reintenta en el siguiente timer).
- ``post``: por defecto no se toca (recodificar 20-30 min de audio en una Pi es caro
  y con pérdidas); ``params.loudnorm: true`` activa ``ctx.post``.
- ``register``: ``data/stock/music/<id>.<ext>`` + fila con ``meta``: title, guid,
  published, link, description (texto plano, para el grounding de ``host_intro``),
  artist y tags (``source:tiny_desk`` y ``artist:<slug>`` si se deduce del título).
- ``finish``: tope de caché (``max_cache_items`` y/o ``max_cache_mb``) por LRU.

Si la red falla, el stock no se toca y la radio sigue sonando (invariante 2).

Parámetros (``params``): ``feed_url`` (obligatorio), ``max_cache_items`` (60),
``max_cache_mb`` (sin límite), ``max_per_run`` (5), ``rotate_per_run`` (1),
``max_download_mb`` (400), ``download_delay_s`` (1.0), ``loudnorm`` (false).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from radio.core.config import RadioConfig
from radio.core.models import AudioInfo, Segment, SegmentKind
from radio.core.paths import tmp_dir
from radio.music.cache import evict_music_cache
from radio.music.feed import (
    TIMEOUT,
    USER_AGENT,
    FeedCache,
    FeedEntry,
    FeedError,
    artist_from_title,
    fetch_feed,
    parse_entries,
)
from radio.music.library import slugify
from radio.producers.base import (
    Draft,
    DraftRejected,
    ProducerContext,
    ProducerError,
    StagedProducer,
)
from radio.producers.post import audio_duration

logger = logging.getLogger(__name__)

MISSING_FEED_URL = "feed_url no configurado (decisión abierta #8)"
SOURCE_TAG = "source:tiny_desk"
CHUNK = 64 * 1024
MB = 1024 * 1024
# Tipos aceptados además de audio/* (CDNs que sirven binario genérico)
_GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream"}
# Códigos que indican un problema del episodio, no de la red: se descarta y se recuerda
_ITEM_ERRORS = {401, 403, 404, 410, 451}

DEFAULTS: dict[str, Any] = {
    "max_cache_items": 60,
    "max_cache_mb": None,
    "max_per_run": 5,
    "rotate_per_run": 1,
    "max_download_mb": 400,
    "download_delay_s": 1.0,
    "loudnorm": False,
}


class MusicTinyDeskProducer(StagedProducer):
    """Descarga episodios del feed RSS oficial como segmentos ``music``."""
    name = "music_tinydesk"
    kind: SegmentKind = "music"
    factual = False
    billable = False            # no gasta en APIs: la regla de gasto no le aplica
    default_target_stock = 30

    def __init__(
        self,
        config: RadioConfig | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        super().__init__(config)
        self._external_client = client
        self._client: httpx.Client | None = None
        self._sleep = sleep
        self._cache: FeedCache | None = None
        self._downloads = 0

    # ── Parámetros ────────────────────────────────────────────────────────────

    def param(self, key: str) -> Any:
        value = self.params.get(key, DEFAULTS[key])
        return DEFAULTS[key] if value is None and DEFAULTS[key] is not None else value

    @property
    def feed_url(self) -> str | None:
        url = self.params.get("feed_url")
        return str(url).strip() if url else None

    # ── Ejecución ─────────────────────────────────────────────────────────────

    def produce(self, ctx: ProducerContext) -> list[Segment]:
        self.configure(ctx.config)
        url = self.feed_url
        if not url:
            raise ProducerError(MISSING_FEED_URL)
        self._cache = FeedCache.load(ctx.data_dir / "cache" / "feeds", self.name, url)
        self._downloads = 0
        own_client = self._external_client is None
        self._client = self._external_client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True
        )
        try:
            return super().produce(ctx)
        finally:
            if own_client:
                self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise ProducerError("cliente HTTP no inicializado (usa produce())")
        return self._client

    @property
    def cache(self) -> FeedCache:
        if self._cache is None:
            raise ProducerError("caché del feed no inicializada (usa produce())")
        return self._cache

    def how_many(self, deficit: int) -> int:
        """Déficit, con un mínimo de rotación y un máximo por ejecución."""
        return min(int(self.param("max_per_run")), max(deficit, int(self.param("rotate_per_run"))))

    def gather(self, ctx: ProducerContext, wanted: int) -> list[Draft]:
        url = self.feed_url
        assert url is not None
        try:
            response = fetch_feed(self.client, url, self.cache)
            entries = parse_entries(response.body)
        except FeedError as exc:
            raise ProducerError(str(exc)) from exc
        if not response.not_modified:
            self.cache.store(response.body, response.etag, response.last_modified)

        # Todos los candidatos, del más reciente al más antiguo: si uno se descarta,
        # se prueba el siguiente (``produce`` para al llegar a ``wanted``)
        drafts: list[Draft] = []
        for entry in entries:
            if entry.guid in self.cache.rejected:
                continue
            if ctx.db.find_by_meta(self.kind, "guid", entry.guid) is not None:
                continue
            drafts.append(self.draft_for(entry))
        logger.info("%s: %d episodios con audio en el feed, %d nuevos; se quieren %d",
                    self.name, len(entries), len(drafts), wanted)
        return drafts

    def draft_for(self, entry: FeedEntry) -> Draft:
        """Borrador con los metadatos del episodio (el audio llega en ``tts``)."""
        artist = artist_from_title(entry.title)
        tags = [SOURCE_TAG]
        if artist and slugify(artist):
            tags.append(f"artist:{slugify(artist)}")
        meta: dict[str, Any] = {
            "title": entry.title,
            "guid": entry.guid,
            "published": entry.published.isoformat() if entry.published else None,
            "link": entry.link,
            "description": entry.description,
            "artist": artist,
            "tags": tags,
            "source": "rss",
            "enclosure_url": entry.audio_url,
        }
        return Draft(ext=entry.ext, meta=meta)

    # ── Etapa de audio: descarga ──────────────────────────────────────────────

    def tts(self, ctx: ProducerContext, draft: Draft) -> Draft:
        """En música no hay locución: esta etapa descarga y verifica el enclosure."""
        if self._downloads and float(self.param("download_delay_s")) > 0:
            self._sleep(float(self.param("download_delay_s")))
        self._downloads += 1
        url = str(draft.meta["enclosure_url"])
        max_bytes = int(float(self.param("max_download_mb")) * MB)
        tmp = tmp_dir(ctx.data_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp / f"{self.kind}-{draft.id}{draft.ext}"
        draft.audio = AudioInfo(path=tmp_path, duration_s=0.0)   # para limpiar si falla

        with self.client.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            follow_redirects=True,
        ) as resp:
            if resp.status_code in _ITEM_ERRORS:
                raise DraftRejected(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise ProducerError(f"descarga de {url}: HTTP {resp.status_code}")
            mime = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if not (mime.startswith("audio/") or mime in _GENERIC_TYPES):
                raise DraftRejected(f"tipo de contenido no es audio: {mime or '(vacío)'}")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise DraftRejected(f"demasiado grande ({int(declared)} bytes)")
            size = 0
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_bytes(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise DraftRejected(f"demasiado grande (> {max_bytes} bytes)")
                    fh.write(chunk)
        if size == 0:
            raise DraftRejected("descarga vacía")
        duration = audio_duration(tmp_path)
        if duration <= 0:
            raise DraftRejected("audio ilegible o de duración 0")
        draft.audio = AudioInfo(path=tmp_path, duration_s=duration)
        return draft

    def post(self, ctx: ProducerContext, draft: Draft) -> Draft:
        if not self.param("loudnorm"):
            return draft
        return super().post(ctx, draft)

    def on_rejected(self, ctx: ProducerContext, draft: Draft, reason: str) -> None:
        """Recuerda el episodio defectuoso para no volver a descargarlo."""
        guid = draft.meta.get("guid")
        if guid:
            self.cache.rejected[str(guid)] = reason
            self.cache.save()

    # ── Tope de caché ─────────────────────────────────────────────────────────

    def finish(self, ctx: ProducerContext, created: list[Segment]) -> None:
        max_items = self.param("max_cache_items")
        if max_items is not None and int(max_items) < self.target_stock:
            logger.warning(
                "%s: max_cache_items (%s) < target_stock (%s); se usa target_stock",
                self.name, max_items, self.target_stock,
            )
            max_items = self.target_stock
        max_mb = self.param("max_cache_mb")
        evict_music_cache(
            ctx.db,
            producer=self.name,
            max_items=None if max_items is None else int(max_items),
            max_mb=None if max_mb is None else float(max_mb),
            protect={s.id for s in created},
        )

    def prepare(self, ctx: ProducerContext, now: datetime) -> None:
        """Nada que caducar: la música no caduca (§14)."""
