"""
Tope de la caché de música descargada (§8: "disco lleno → tope de tamaño de caché
de música (LRU)").

Solo se consideran los segmentos ``music`` en ``ready`` creados por el productor
indicado: nunca se borran archivos importados a mano (``radio import-music``).

Orden de expulsión (LRU): por "última actividad" = la última emisión en
``play_log`` o, si nunca ha sonado, su ``created_at``. Así lo emitido hace más
tiempo sale primero y, entre lo nunca emitido, lo más antiguo; lo recién
descargado es lo último en salir. Al expulsar, primero se marca ``retired`` (nunca
queda una fila ``ready`` apuntando a un archivo borrado) y luego se borra el audio.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime

from radio.core.models import Segment
from radio.core.store import DB

logger = logging.getLogger(__name__)

MB = 1024 * 1024


@dataclass
class EvictionReport:
    """Segmentos retirados y bytes liberados."""
    retired: list[str] = field(default_factory=list)
    freed_bytes: int = 0


def _size(seg: Segment) -> int:
    try:
        return seg.path.stat().st_size
    except OSError:
        return 0


def last_activity(db: DB, segments: Collection[Segment]) -> dict[str, datetime]:
    """Por id: última emisión según ``play_log`` o, si no hay, ``created_at``."""
    activity = {s.id: s.created_at for s in segments}
    if not segments:
        return activity
    since = min(activity.values())   # no puede haber emisiones antes de su creación
    for entry in db.list_play_log(since=since):
        if entry.segment_id in activity and entry.started_at > activity[entry.segment_id]:
            activity[entry.segment_id] = entry.started_at
    return activity


def evict_music_cache(
    db: DB,
    *,
    producer: str,
    max_items: int | None,
    max_mb: float | None = None,
    protect: Collection[str] = (),
) -> EvictionReport:
    """
    Retira segmentos de ``producer`` hasta cumplir ``max_items`` y ``max_mb``
    (None = sin límite). Los ids de ``protect`` no se tocan.
    """
    report = EvictionReport()
    ready = [s for s in db.list_segments(kind="music", status="ready") if s.producer == producer]
    activity = last_activity(db, ready)
    sizes = {s.id: _size(s) for s in ready}
    count, total = len(ready), sum(sizes.values())

    def over() -> bool:
        return (max_items is not None and count > max_items) or (
            max_mb is not None and total > max_mb * MB
        )

    for seg in sorted(ready, key=lambda s: (activity[s.id], s.id)):
        if not over():
            break
        if seg.id in protect:
            continue
        db.update_segment_status(seg.id, "retired")
        seg.path.unlink(missing_ok=True)
        count -= 1
        total -= sizes[seg.id]
        report.retired.append(seg.id)
        report.freed_bytes += sizes[seg.id]
        logger.info("Caché de música: retirado %s (%s)", seg.id, seg.title)
    return report
