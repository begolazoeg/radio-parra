"""
Espejo de la cola del reproductor (§4.4): qué ha encolado la emisora y en qué orden.

El backend de audio solo conoce rutas (§1 inv. 1); la emisora necesita saber, para
cada archivo encolado, qué segmento y qué unidad hay detrás, cuándo empezó y cuándo
está previsto que empiece lo siguiente (para programar el *lookahead* con horas
proyectadas). ``AirQueue`` guarda eso y casa los eventos ``Started``/``Ended`` con sus
elementos, en el mismo orden FIFO que usa el backend.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from radio.core.models import Segment
from radio.grid.scheduler import PlayUnit

logger = logging.getLogger(__name__)

# Kind con el que se registra en play_log el bucle de emergencia (sin segmento)
EMERGENCY_KIND = "emergency"


@dataclass(eq=False)
class QueueItem:
    """
    Un archivo encolado en el reproductor (identidad por objeto).

    ``segment`` None = bucle de emergencia. ``unit_no`` agrupa los archivos de una
    misma ``PlayUnit`` (p. ej. ``[host_intro, music]``). ``play_id`` es la fila de
    ``play_log`` abierta al empezar. ``cut`` marca lo que la emisora cortó para dar
    paso a una interrupción.
    """
    path: Path
    kind: str
    duration_s: float
    unit_no: int
    segment: Segment | None = None
    unit: PlayUnit | None = None
    play_id: int | None = None
    started_at: datetime | None = None
    cut: bool = False

    @property
    def is_emergency(self) -> bool:
        return self.segment is None

    @property
    def title(self) -> str:
        return self.segment.title if self.segment is not None else f"emergencia ({self.path.name})"


class AirQueue:
    """Lo que suena (``current``) y lo pendiente (``pending``), en orden."""

    def __init__(self) -> None:
        self.current: QueueItem | None = None
        self.pending: deque[QueueItem] = deque()

    # ── Altas y bajas ────────────────────────────────────────────────────────

    def push(self, item: QueueItem) -> None:
        self.pending.append(item)

    def drop_pending(self) -> list[QueueItem]:
        """Vacía lo pendiente y devuelve lo que había."""
        dropped = list(self.pending)
        self.pending.clear()
        return dropped

    def on_started(self, path: Path) -> QueueItem | None:
        """Casa un ``Started``: el primer pendiente (si la ruta no coincide, se busca)."""
        item = self._take_pending(path)
        if item is None:
            return None
        if self.current is not None:
            logger.warning("Empieza %s sin haber terminado %s", path, self.current.path)
        self.current = item
        return item

    def on_ended(self, path: Path) -> QueueItem | None:
        """
        Casa un ``Ended``: lo que suena o, si no suena nada con esa ruta, un pendiente
        que falló antes de empezar (``Ended`` sin ``Started``).
        """
        if self.current is not None and self.current.path == path:
            item, self.current = self.current, None
            return item
        return self._take_pending(path)

    def _take_pending(self, path: Path) -> QueueItem | None:
        for i, item in enumerate(self.pending):
            if item.path == path:
                if i:
                    logger.warning("Cola desalineada: %s no era el primero pendiente", path)
                del self.pending[i]
                return item
        logger.warning("Evento de un archivo que no está en la cola: %s", path)
        return None

    # ── Consultas ────────────────────────────────────────────────────────────

    def items(self) -> list[QueueItem]:
        """Lo que suena y lo pendiente, en orden."""
        return ([self.current] if self.current is not None else []) + list(self.pending)

    def pending_units(self) -> int:
        """Unidades con algún archivo pendiente (el *lookahead* efectivo)."""
        return len({item.unit_no for item in self.pending})

    def current_end(self, now: datetime) -> datetime:
        """Fin previsto de lo que suena (``now`` si no suena nada o ya debía haber acabado)."""
        cur = self.current
        if cur is None or cur.started_at is None:
            return now
        return max(now, cur.started_at + timedelta(seconds=cur.duration_s))

    def pending_by_unit(self, now: datetime) -> list[tuple[list[QueueItem], datetime]]:
        """Pendientes agrupados por unidad, con la hora proyectada de inicio de cada grupo."""
        groups: list[tuple[list[QueueItem], datetime]] = []
        t = self.current_end(now)
        for item in self.pending:
            if groups and groups[-1][0][-1].unit_no == item.unit_no:
                groups[-1][0].append(item)
            else:
                groups.append(([item], t))
            t += timedelta(seconds=item.duration_s)
        return groups

    def tail_time(self, now: datetime) -> datetime:
        """Hora proyectada a la que acabará todo lo encolado."""
        t = self.current_end(now)
        for item in self.pending:
            t += timedelta(seconds=item.duration_s)
        return t
