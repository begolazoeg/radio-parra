"""
Tests unitarios para las implementaciones de Clock.
"""

from __future__ import annotations

from datetime import datetime

from radio.core.clock import FakeClock, SystemClock


def test_fake_clock_advance() -> None:
    """advance(60) sube exactamente 60 segundos al reloj fake."""
    start = datetime(2024, 6, 1, 12, 0, 0)
    clock = FakeClock(start)
    assert clock.now() == start

    clock.advance(60)
    expected = datetime(2024, 6, 1, 12, 1, 0)
    assert clock.now() == expected


def test_fake_clock_multiple_advances() -> None:
    """Varios advance() se acumulan correctamente."""
    clock = FakeClock(datetime(2024, 1, 1, 0, 0, 0))
    clock.advance(3600)   # +1h
    clock.advance(1800)   # +30m
    assert clock.now() == datetime(2024, 1, 1, 1, 30, 0)


def test_system_clock_returns_datetime() -> None:
    """SystemClock.now() devuelve una instancia de datetime."""
    clock = SystemClock()
    result = clock.now()
    assert isinstance(result, datetime)


def test_system_clock_is_timezone_aware() -> None:
    """SystemClock devuelve datetimes aware (UTC por defecto o la zona pedida)."""
    assert SystemClock().now().tzinfo is not None
    madrid = SystemClock("Europe/Madrid").now()
    assert madrid.tzinfo is not None
    assert str(madrid.tzinfo) == "Europe/Madrid"
