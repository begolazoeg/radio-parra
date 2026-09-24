"""
Ejecutor de producers: decide cuáles tocan según producers.yaml y
registra cada ejecución en producer_runs. Un producer que falla no
impide que se ejecuten los demás.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from radio.producers.base import Producer, ProducerContext

logger = logging.getLogger(__name__)


class ProducerRunner:
    """Ejecuta periódicamente los producers activos."""

    def __init__(self, ctx: ProducerContext, producers: Sequence[Producer]) -> None:
        self.ctx = ctx
        self.producers = list(producers)

    def _is_due(self, producer: Producer, now: datetime) -> bool:
        """Un producer toca si está activo y ha pasado su intervalo desde la última ejecución."""
        settings = self.ctx.config.producers.producers.get(producer.name)
        if settings is None or not settings.active:
            return False
        last = self.ctx.db.last_producer_run(producer.name)
        if last is None or settings.interval_minutes <= 0:
            return True
        started = datetime.fromisoformat(str(last["started_at"]))
        # Normaliza naive/aware para poder restar sin errores
        if started.tzinfo is None and now.tzinfo is not None:
            started = started.replace(tzinfo=now.tzinfo)
        elif started.tzinfo is not None and now.tzinfo is None:
            started = started.replace(tzinfo=None)
        return now - started >= timedelta(minutes=settings.interval_minutes)

    def due(self) -> list[Producer]:
        """Producers que deben ejecutarse ahora."""
        now = self.ctx.clock.now()
        return [p for p in self.producers if self._is_due(p, now)]

    def tick(self) -> dict[str, list[str]]:
        """Ejecuta todos los producers que tocan. Devuelve nombre -> ids creados."""
        results: dict[str, list[str]] = {}
        for producer in self.due():
            started_at = self.ctx.clock.now().isoformat()
            try:
                created = producer.run(self.ctx)
            except Exception as exc:
                logger.exception("Producer %s ha fallado", producer.name)
                self.ctx.db.log_producer_run(
                    producer.name,
                    started_at=started_at,
                    ended_at=self.ctx.clock.now().isoformat(),
                    status="error",
                    detail=repr(exc),
                )
                results[producer.name] = []
                continue
            self.ctx.db.log_producer_run(
                producer.name,
                started_at=started_at,
                ended_at=self.ctx.clock.now().isoformat(),
                status="ok",
                detail=f"{len(created)} segmentos creados",
            )
            results[producer.name] = created
        return results
