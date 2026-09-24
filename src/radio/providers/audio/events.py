"""
Eventos de reproducción que emiten los backends de audio con cola.

La emisora los usa para escribir ``play_log`` al empezar (``Started``) y al terminar
(``Ended``) cada archivo (ARCHITECTURE.md §4.4). Los backends solo informan de
*archivos*: no saben qué segmento, kind o unidad hay detrás (§1 inv. 1).

Contrato
--------
- Cada archivo encolado produce como mucho un ``Started`` y exactamente un ``Ended``
  (salvo que se cierre el backend antes de que empiece: entonces no produce ninguno).
- Un ``Ended`` puede llegar sin ``Started`` previo si el archivo no llegó a sonar
  (p. ej. el reproductor murió justo al cargarlo): siempre con ``reason="error"``.
- Los oyentes se llaman **desde el hilo del backend** (lector IPC o watchdog), en
  orden y sin tener tomado ningún lock interno: pueden llamar de vuelta al backend
  (``enqueue``, ``queued``...), pero deben ser rápidos. Una excepción en un oyente
  se registra y no afecta al resto.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

EndReason = Literal["eof", "skipped", "error"]


@dataclass(frozen=True)
class Started:
    """El archivo ``path`` ha empezado a sonar en ``at``."""

    path: Path
    at: datetime


@dataclass(frozen=True)
class Ended:
    """
    El archivo ``path`` ha dejado de sonar en ``at``.

    ``reason``: ``"eof"`` (terminó entero), ``"skipped"`` (cortado por ``skip()`` o al
    cerrar el backend) o ``"error"`` (no se pudo reproducir o el reproductor murió).
    """

    path: Path
    at: datetime
    reason: EndReason


PlayerEvent = Started | Ended
EventListener = Callable[[PlayerEvent], None]


class ListenerSet:
    """Conjunto de oyentes con alta/baja segura entre hilos y despacho tolerante a fallos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list[EventListener] = []

    def add(self, listener: EventListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove(self, listener: EventListener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def emit(self, events: Iterable[PlayerEvent]) -> None:
        """Entrega ``events`` en orden a cada oyente. Llamar sin locks del backend tomados."""
        with self._lock:
            listeners = list(self._listeners)
        for event in events:
            for listener in listeners:
                try:
                    listener(event)
                except Exception:
                    log.exception("Oyente de eventos de audio falló con %r", event)


class EventRecorder:
    """
    Oyente que guarda los eventos en una cola, para quien prefiera *consumir* eventos
    en su propio hilo en lugar de recibir callbacks::

        rec = EventRecorder()
        backend.add_listener(rec)
        ev = rec.get(timeout=5)       # bloquea sin espera activa

    También conserva el historial completo en ``rec.events`` (útil en tests).
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[PlayerEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._history: list[PlayerEvent] = []

    def __call__(self, event: PlayerEvent) -> None:
        with self._lock:
            self._history.append(event)
        self._queue.put(event)

    @property
    def events(self) -> list[PlayerEvent]:
        """Copia de todos los eventos recibidos hasta ahora."""
        with self._lock:
            return list(self._history)

    def get(self, timeout: float | None = None) -> PlayerEvent | None:
        """Siguiente evento no consumido; ``None`` si no llega ninguno en ``timeout`` s."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
