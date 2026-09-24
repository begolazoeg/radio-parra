"""
Reglas de la parrilla (§4.3 y §5): franjas horarias e interrupciones.

Todo es puro (sin I/O) y trabaja con fechas *aware*. Las horas de la parrilla son
hora local de ``grid.timezone``; la aritmética de instantes se hace en UTC para
que los cambios de horario (DST) no descuadren nada.

Expresiones ``when``
--------------------
No se usa ``eval``. La gramática admitida es mínima:

- ``minute == N``            (N entre 0 y 59)
- ``minute in [N, M, ...]``  (lista no vacía de minutos 0–59)

Cualquier otra cosa lanza ``ValueError`` al cargar grid.yaml.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from radio.core.config import Daypart, GridConfig, InterruptRule, ModeConfig

# Margen tras la hora en punto durante el que una señal horaria sigue siendo emitible
# (su caducidad, ver producers/time_signal.py). La puntualidad real la marca
# ``max_late_seconds`` de cada regla de interrupción.
TIME_SIGNAL_WINDOW_MIN = 5

# Modo al que se recurre si se pide uno que no existe en grid.yaml
DEFAULT_MODE = "default"

# Franja sintética cuando ninguna franja del modo cubre la hora (solo música)
FALLBACK_DAYPART = Daypart.model_validate(
    {"name": "fuera_de_franja", "from": "00:00", "to": "24:00", "pattern": ["music"]}
)

_WHEN_EQ = re.compile(r"^\s*minute\s*==\s*(\d{1,2})\s*$")
_WHEN_IN = re.compile(r"^\s*minute\s+in\s*\[\s*(\d{1,2}(?:\s*,\s*\d{1,2})*)\s*,?\s*\]\s*$")


# ── Expresiones ``when`` ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class WhenRule:
    """Expresión ``when`` ya analizada: minutos (hora local) en los que salta."""
    minutes: frozenset[int]


def parse_when(expr: str) -> WhenRule:
    """Analiza una expresión ``when`` (ver docstring del módulo). ValueError si no es válida."""
    if not isinstance(expr, str):
        raise ValueError(f"expresión 'when' inválida: {expr!r}")
    m = _WHEN_EQ.match(expr)
    if m:
        values = [int(m.group(1))]
    else:
        m = _WHEN_IN.match(expr)
        if not m:
            raise ValueError(
                f"expresión 'when' no soportada: {expr!r} "
                "(se admite 'minute == N' o 'minute in [N, ...]')"
            )
        values = [int(v) for v in m.group(1).split(",")]
    bad = [v for v in values if not 0 <= v <= 59]
    if bad:
        raise ValueError(f"minutos fuera de rango en {expr!r}: {bad}")
    return WhenRule(frozenset(values))


# ── Fechas ────────────────────────────────────────────────────────────────────

def zone(grid: GridConfig) -> ZoneInfo:
    return ZoneInfo(grid.timezone)


def aware(t: datetime, tz: ZoneInfo) -> datetime:
    """Fecha aware; una naive se interpreta como hora local de la parrilla."""
    return t.replace(tzinfo=tz) if t.tzinfo is None else t


def parse_hhmm(value: str) -> int:
    """"HH:MM" → minutos desde medianoche ("24:00" → 1440)."""
    hours, minutes = value.strip().split(":")
    return int(hours) * 60 + int(minutes)


# ── Modos y franjas ───────────────────────────────────────────────────────────

def resolve_mode(grid: GridConfig, mode: str) -> tuple[str, ModeConfig]:
    """Modo pedido; si no existe, ``default``; si tampoco, el primero del archivo."""
    if mode in grid.modes:
        return mode, grid.modes[mode]
    if DEFAULT_MODE in grid.modes:
        return DEFAULT_MODE, grid.modes[DEFAULT_MODE]
    name = next(iter(grid.modes))
    return name, grid.modes[name]


def daypart_contains(part: Daypart, minute_of_day: int) -> bool:
    """¿Cae el minuto del día (hora local) en la franja? Soporta cruce de medianoche."""
    start = parse_hhmm(part.from_)
    end = parse_hhmm(part.to)
    if start == end or (start == 0 and end == 24 * 60):
        return True
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def daypart_for(grid: GridConfig, now: datetime, mode: str) -> Daypart:
    """Franja activa en ``now`` (hora local de pared) para ``mode``; la primera que encaje."""
    tz = zone(grid)
    local = aware(now, tz).astimezone(tz)
    minute_of_day = local.hour * 60 + local.minute
    _, cfg = resolve_mode(grid, mode)
    for part in cfg.dayparts:
        if daypart_contains(part, minute_of_day):
            return part
    return FALLBACK_DAYPART


# ── Interrupciones ────────────────────────────────────────────────────────────

def _fire_instants(rule: InterruptRule, around: datetime, tz: ZoneInfo) -> Iterator[datetime]:
    """
    Instantes (aware, UTC) en que salta la regla entre 2 h antes y 2 h después de
    ``around``. Las horas se recorren en UTC (DST-safe) y cada candidato se verifica
    contra el minuto local, así sirven también zonas con desfase no entero.
    """
    when = parse_when(rule.when)
    local = around.astimezone(tz)
    hour_start = local.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
    for k in range(-2, 3):
        base = hour_start + timedelta(hours=k)
        for minute in sorted(when.minutes):
            t = base + timedelta(minutes=minute)
            if t.astimezone(tz).minute == minute:
                yield t


def last_fire_at(rule: InterruptRule, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Último instante <= ``now`` en que saltó la regla (en las 2 h previas)."""
    now = aware(now, tz)
    past = [t for t in _fire_instants(rule, now, tz) if t <= now]
    return max(past) if past else None


def next_fire_at(rule: InterruptRule, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Próximo instante > ``now`` en que salta la regla."""
    now = aware(now, tz)
    future = [t for t in _fire_instants(rule, now, tz) if t > now]
    return min(future) if future else None


def next_interrupt_at(grid: GridConfig, now: datetime, mode: str) -> datetime | None:
    """
    Próximo instante (> ``now``) en que salta alguna regla de interrupción de ``mode``,
    o None si el modo no tiene interrupciones. Pensado para que la emisora programe
    el corte del audio en curso (la puntualidad de la señal horaria es cosa suya).
    """
    tz = zone(grid)
    _, cfg = resolve_mode(grid, mode)
    times = [t for r in cfg.interrupts if (t := next_fire_at(r, now, tz)) is not None]
    return min(times).astimezone(tz) if times else None


def fires_per_window(rule: InterruptRule, window_minutes: float) -> int:
    """Cota superior de veces que salta la regla dentro de una ventana de N minutos."""
    per_hour = len(parse_when(rule.when).minutes)
    return math.ceil(window_minutes / 60.0) * per_hour


# ── Etiquetas horarias ────────────────────────────────────────────────────────

HOUR_TAG_PREFIX = "hour:"


def hour_tag(dt: datetime, tz: ZoneInfo | None = None) -> str:
    """
    Etiqueta ``hour:YYYY-MM-DDTHH`` de la hora local de ``dt`` (la misma convención que
    ``producers.time_signal.hour_tag``). Con ``tz`` se convierte antes a esa zona.
    """
    local = dt.astimezone(tz) if tz is not None else dt
    return HOUR_TAG_PREFIX + local.strftime("%Y-%m-%dT%H")
