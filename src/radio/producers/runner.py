"""
Ejecutor de producers: decide cuáles tocan según producers.yaml y
registra cada ejecución en producer_runs. Un producer que falla no
impide que se ejecuten los demás.

Un producer toca si está ``active``, tiene ``cron`` y hay algún disparo del cron
(en hora local de la emisora) desde el inicio de su última ejecución; la primera
vez toca siempre. Sin ``cron`` solo se ejecuta a mano.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from radio.core.cron import cron_due
from radio.producers.base import Producer, ProducerContext

logger = logging.getLogger(__name__)


class ProducerRunner:
    """Ejecuta periódicamente los producers activos."""

    def __init__(self, ctx: ProducerContext, producers: Sequence[Producer]) -> None:
        self.ctx = ctx
        self.producers = list(producers)
        self._tz = ZoneInfo(ctx.config.station.timezone)

    def _now(self) -> datetime:
        now = self.ctx.clock.now()
        return now.replace(tzinfo=self._tz) if now.tzinfo is None else now

    def _is_due(self, producer: Producer, now: datetime) -> bool:
        """Activo, con cron y con algún disparo desde la última ejecución."""
        settings = self.ctx.config.producers.get(producer.name)
        if settings is None or not settings.active or settings.cron is None:
            return False
        last = self.ctx.db.last_producer_run(producer.name)
        return cron_due(
            settings.cron,
            None if last is None else last.started_at,
            now.astimezone(self._tz),
        )

    def due(self) -> list[Producer]:
        """Producers que deben ejecutarse ahora."""
        now = self._now()
        return [p for p in self.producers if self._is_due(p, now)]

    def tick(self) -> dict[str, list[str]]:
        """Ejecuta todos los producers que tocan. Devuelve nombre -> ids creados."""
        results: dict[str, list[str]] = {}
        for producer in self.due():
            run_id = self.ctx.db.start_producer_run(producer.name, self._now())
            try:
                created = producer.run(self.ctx)
            except Exception as exc:
                logger.exception("Producer %s ha fallado", producer.name)
                self.ctx.db.finish_producer_run(
                    run_id, ended_at=self._now(), ok=False, error=repr(exc)
                )
                results[producer.name] = []
                continue
            self.ctx.db.finish_producer_run(
                run_id, ended_at=self._now(), ok=True, n_segments=len(created)
            )
            results[producer.name] = created
        return results
