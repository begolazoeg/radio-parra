"""
Expresiones cron mínimas (5 campos) para ``producers.yaml``.

Soporta ``*``, valores, rangos ``a-b``, listas ``a,b`` y pasos ``*/n`` / ``a-b/n``
en los campos minuto, hora, día del mes, mes y día de la semana (0 o 7 = domingo).
Como en cron clásico, si día del mes y día de la semana están restringidos, basta
con que coincida uno de los dos. Las fechas se evalúan en la zona que traigan
(el llamador las pasa en la zona de la emisora).
"""

from __future__ import annotations

from datetime import datetime, timedelta

# (mínimo, máximo) de cada campo
_FIELDS: tuple[tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

# Tope de búsqueda de disparos pendientes (evita bucles largos tras un apagón)
_MAX_SCAN = timedelta(days=2)


def _parse_field(spec: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_txt = part.split("/", 1)
            step = int(step_txt)
            if step <= 0:
                raise ValueError(f"Paso no válido en cron: {spec!r}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if step != 1:
                end = hi
        if not (lo <= start <= end <= hi):
            raise ValueError(f"Valor fuera de rango en cron: {spec!r} ({lo}-{hi})")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    """Parsea una expresión de 5 campos; lanza ValueError si no es válida."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"La expresión cron debe tener 5 campos: {expr!r}")
    try:
        parsed = tuple(
            _parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELDS, strict=True)
        )
    except ValueError as exc:
        raise ValueError(f"Expresión cron no válida {expr!r}: {exc}") from exc
    return parsed


def _matches(parsed: tuple[frozenset[int], ...], restricted_days: bool, t: datetime) -> bool:
    minute, hour, dom, month, dow = parsed
    weekday = (t.weekday() + 1) % 7          # cron: 0 = domingo
    dow_ok = weekday in dow or (weekday == 0 and 7 in dow)
    dom_ok = t.day in dom
    day_ok = (dom_ok or dow_ok) if restricted_days else (dom_ok and dow_ok)
    return t.minute in minute and t.hour in hour and t.month in month and day_ok


def _restricted_days(expr: str) -> bool:
    fields = expr.split()
    return fields[2] != "*" and fields[4] != "*"


def cron_matches(expr: str, t: datetime) -> bool:
    """¿Dispara ``expr`` en el minuto de ``t``?"""
    return _matches(parse_cron(expr), _restricted_days(expr), t)


def cron_due(expr: str, last: datetime | None, now: datetime) -> bool:
    """
    ¿Hay algún disparo de ``expr`` en ``(last, now]``? Sin ``last`` siempre toca.
    Solo se exploran los últimos dos días (un apagón largo cuenta como un disparo).
    """
    parsed = parse_cron(expr)  # valida aunque no haga falta buscar
    if last is None:
        return True
    restricted = _restricted_days(expr)
    if now.tzinfo is not None:
        last = last.astimezone(now.tzinfo)
    start = max(last, now - _MAX_SCAN)
    t = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now.replace(second=0, microsecond=0)
    while t <= end:
        if _matches(parsed, restricted, t):
            return True
        t += timedelta(minutes=1)
    return False
