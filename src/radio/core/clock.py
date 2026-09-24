"""
Abstracción de reloj para permitir tests deterministas.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo


@runtime_checkable
class Clock(Protocol):
    """Protocolo mínimo de reloj."""

    def now(self) -> datetime:
        ...


class SystemClock:
    """
    Reloj real. Devuelve siempre datetimes con zona horaria (aware),
    por defecto en UTC; pasar `tz` (p. ej. "Europe/Madrid") para hora local.
    """

    def __init__(self, tz: str | tzinfo | None = None) -> None:
        if tz is None:
            self._tz: tzinfo = UTC
        elif isinstance(tz, str):
            self._tz = ZoneInfo(tz)
        else:
            self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FakeClock:
    """
    Reloj controlable para tests.
    Se inicializa con una fecha fija y puede avanzar manualmente.
    """

    def __init__(self, fixed: datetime) -> None:
        self._current = fixed

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        """Avanza el reloj interno en `seconds` segundos."""
        self._current += timedelta(seconds=seconds)
