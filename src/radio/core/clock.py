"""
Abstracción de reloj para permitir tests deterministas.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Protocolo mínimo de reloj."""

    def now(self) -> datetime:
        ...


class SystemClock:
    """Reloj real que delega en datetime.now()."""

    def now(self) -> datetime:
        return datetime.now()


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
