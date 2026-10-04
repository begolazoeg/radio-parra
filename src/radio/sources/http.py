"""
Acceso HTTP educado a las fuentes abiertas.

- ``User-Agent`` descriptivo en todas las peticiones (``USER_AGENT``, mismo estilo que
  ``radio.music.feed``). MusicBrainz lo exige y Wikimedia lo pide.
- ``RateLimiter``: intervalo mínimo entre peticiones **por host**. MusicBrainz:
  1 petición/segundo como máximo (su política); Wikimedia: 0,5 s (petición en serie,
  sin ráfagas). Reloj y ``sleep`` inyectables para tests.
- ``Fetcher.get_json``: caché (si hay) → espera del limitador → GET. 503/429/5xx
  se reintentan con espera exponencial (respeta ``Retry-After`` numérico, con tope).
  404 devuelve None. Cualquier otro fallo (red, estado, JSON) lanza ``SourceError``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from radio.music.feed import USER_AGENT
from radio.sources.cache import SourceCache

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MUSICBRAINZ_INTERVAL_S = 1.0
WIKIMEDIA_INTERVAL_S = 0.5
DEFAULT_INTERVAL_S = 1.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceError(Exception):
    """Fallo al obtener una fuente (red, HTTP o respuesta ilegible)."""


def _default_interval(host: str) -> float:
    if host == "musicbrainz.org" or host.endswith(".musicbrainz.org"):
        return MUSICBRAINZ_INTERVAL_S
    if host.endswith((".wikipedia.org", ".wikidata.org", ".wikimedia.org")):
        return WIKIMEDIA_INTERVAL_S
    return DEFAULT_INTERVAL_S


class RateLimiter:
    """
    Intervalo mínimo entre peticiones al mismo host. Conviene compartir una
    instancia entre llamadas a ``gather_artist_sources`` de una misma ejecución.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        intervals: Mapping[str, float] | None = None,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self._intervals = dict(intervals or {})
        self._last: dict[str, float] = {}

    def interval(self, host: str) -> float:
        """Intervalo mínimo (s) para ``host``."""
        return self._intervals.get(host, _default_interval(host))

    def wait(self, host: str) -> None:
        """Duerme lo necesario para respetar el intervalo de ``host`` y lo marca como usado."""
        last = self._last.get(host)
        if last is not None:
            remaining = last + self.interval(host) - self.clock()
            if remaining > 0:
                self.sleep(remaining)
        self._last[host] = self.clock()


class Fetcher:
    """GET de JSON con User-Agent, caché, límite de tasa y reintentos."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        limiter: RateLimiter,
        cache: SourceCache | None = None,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.cache = cache
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After", "")
        try:
            delay = float(header)
        except ValueError:
            delay = self.backoff_s * (2**attempt)
        return max(0.0, min(delay, self.max_backoff_s))

    def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any | None:
        """
        JSON de ``url`` (con ``params``) o None si el recurso no existe (404).
        Lanza ``SourceError`` ante cualquier otro fallo.
        """
        full = httpx.URL(url, params=dict(params) if params else None)
        key = str(full)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit.body if hit.status == 200 else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(full.host)
            try:
                resp = self.client.get(full, headers=headers, timeout=TIMEOUT,
                                       follow_redirects=True)
            except httpx.HTTPError as exc:
                raise SourceError(f"error de red en {key}: {exc}") from exc
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._retry_delay(resp, attempt)
                logger.info("HTTP %s en %s; reintento en %.1f s", resp.status_code, key, delay)
                self.limiter.sleep(delay)
                continue
            if resp.status_code == 404:
                if self.cache is not None:
                    self.cache.put(key, 404, None)
                return None
            if resp.status_code != 200:
                raise SourceError(f"HTTP {resp.status_code} en {key}")
            try:
                body = resp.json()
            except ValueError as exc:
                raise SourceError(f"JSON ilegible en {key}") from exc
            if self.cache is not None:
                self.cache.put(key, 200, body)
            return body
        raise SourceError(f"sin respuesta válida tras {self.max_retries} reintentos: {key}")
