"""
Presupuesto de charla (invariante §1.8, paso 3 de §4.3).

Qué cuenta como "palabra"
-------------------------
Todo lo que no sea música, identificativo sonoro o bucle de emergencia:
``NON_TALK_KINDS`` = {music, jingle, stinger, emergency}. La señal horaria, las
intros y cualquier kind nuevo cuentan como palabra (un kind desconocido se trata como
palabra, que es lo prudente).

Cómo se mide
------------
La proporción es ``segundos de palabra en la ventana / duración de la ventana``
(``window_minutes``). El denominador es la ventana completa, no el tiempo emitido:
en régimen normal la antena nunca está vacía y ambos coinciden; al arrancar (sin
historial) no bloquea la palabra por falta de datos.

Comprobación proyectada
-----------------------
Antes de emitir una unidad se comprueba, para cada segmento de palabra que contiene,
la ventana que **termina al final de ese segmento** (historial recortado + lo ya
planificado de la unidad). Si todas cumplen, cualquier ventana posterior también
cumple salvo por las interrupciones que salten después; para ellas se reserva
``reserve_s`` segundos (ver ``interrupt_reserve_s`` en el scheduler). Así la
propiedad "nunca se supera el presupuesto" se mantiene de forma estricta.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from radio.core.models import PlayLogEntry

# Kinds que no consumen presupuesto de charla
# (``emergency``: kind sugerido para registrar en play_log el bucle de emergencia)
NON_TALK_KINDS: frozenset[str] = frozenset({"music", "jingle", "stinger", "emergency"})

# Tolerancia numérica al comparar proporciones (segundos)
_EPS_S = 1e-6


def is_talk(kind: str) -> bool:
    """¿Consume presupuesto de charla este kind?"""
    return kind not in NON_TALK_KINDS


@dataclass(frozen=True)
class Interval:
    """Tramo de antena ya emitido o planificado."""
    start: datetime
    end: datetime
    talk: bool


def entry_interval(entry: PlayLogEntry) -> Interval:
    """Tramo de una fila de play_log: fin real si está cerrada, si no el nominal."""
    if entry.ended_at is not None:
        end = entry.ended_at
    else:
        end = entry.started_at + timedelta(seconds=entry.duration_s or 0.0)
    return Interval(entry.started_at, end, is_talk(entry.kind))


def talk_seconds(intervals: Iterable[Interval], start: datetime, end: datetime) -> float:
    """Segundos de palabra dentro de ``[start, end]``."""
    total = 0.0
    for iv in intervals:
        if not iv.talk:
            continue
        clipped = (min(iv.end, end) - max(iv.start, start)).total_seconds()
        if clipped > 0:
            total += clipped
    return total


def talk_ratio(history: Sequence[PlayLogEntry], now: datetime, window: timedelta) -> float:
    """Proporción de palabra en la ventana que termina en ``now``."""
    intervals = [entry_interval(e) for e in history]
    return talk_seconds(intervals, now - window, now) / window.total_seconds()


def fits_budget(
    history: Sequence[PlayLogEntry] | Sequence[Interval],
    planned: Sequence[tuple[str, float]],
    now: datetime,
    *,
    window: timedelta,
    max_ratio: float,
    reserve_s: float = 0.0,
) -> bool:
    """
    ¿Se puede emitir desde ``now`` la secuencia ``planned`` (``(kind, duración)``) sin
    que ninguna ventana que termine en un segmento de palabra supere ``max_ratio``
    (dejando ``reserve_s`` para interrupciones futuras)? ``history`` puede venir ya
    convertido a ``Interval`` para no recalcularlo en cada candidato.
    """
    if not any(is_talk(kind) for kind, _ in planned):
        return True
    limit = max_ratio * window.total_seconds() - reserve_s
    intervals = [
        iv for iv in (h if isinstance(h, Interval) else entry_interval(h) for h in history)
        if iv.talk and iv.end > now - window
    ]
    cursor = now
    for kind, duration in planned:
        end = cursor + timedelta(seconds=duration)
        iv = Interval(cursor, end, is_talk(kind))
        intervals.append(iv)
        if iv.talk and talk_seconds(intervals, end - window, end) > limit + _EPS_S:
            return False
        cursor = end
    return True
