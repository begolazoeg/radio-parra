"""
Tests del evaluador mínimo de cron (producers.yaml).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from radio.core.cron import cron_due, cron_matches, parse_cron

MADRID = ZoneInfo("Europe/Madrid")


def t(h: int, m: int, day: int = 5) -> datetime:
    return datetime(2026, 1, day, h, m, tzinfo=MADRID)   # 5/1/2026 es lunes


@pytest.mark.parametrize(
    ("expr", "when", "expected"),
    [
        ("*/30 * * * *", t(10, 0), True),
        ("*/30 * * * *", t(10, 30), True),
        ("*/30 * * * *", t(10, 15), False),
        ("0 */6 * * *", t(6, 0), True),
        ("0 */6 * * *", t(7, 0), False),
        ("5,10-12 8 * * *", t(8, 11), True),
        ("5,10-12 8 * * *", t(8, 13), False),
        ("0 9 * * 1-5", t(9, 0), True),            # lunes
        ("0 9 * * 0", t(9, 0, day=4), True),       # domingo
        ("0 9 * * 7", t(9, 0, day=4), True),       # 7 = domingo
        ("0 9 1 * 1", t(9, 0), True),              # día del mes O día de la semana
        ("0 9 1 * 2", t(9, 0), False),
    ],
)
def test_cron_matches(expr: str, when: datetime, expected: bool) -> None:
    assert cron_matches(expr, when) is expected


@pytest.mark.parametrize("expr", ["", "* * * *", "60 * * * *", "*/0 * * * *", "a * * * *",
                                  "5-1 * * * *"])
def test_parse_cron_rejects(expr: str) -> None:
    with pytest.raises(ValueError):
        parse_cron(expr)


def test_cron_due() -> None:
    expr = "*/30 * * * *"
    assert cron_due(expr, None, t(10, 7))
    assert not cron_due(expr, t(10, 0), t(10, 29))
    assert cron_due(expr, t(10, 0), t(10, 30))
    assert cron_due(expr, t(10, 0), t(10, 31))
    # El disparo en el mismo minuto que la última ejecución no cuenta dos veces
    assert not cron_due(expr, t(10, 0) + timedelta(seconds=20), t(10, 0) + timedelta(seconds=50))
    # last en otra zona horaria (UTC): 09:00 UTC = 10:00 Madrid
    assert not cron_due(expr, datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("UTC")), t(10, 20))
    # Tras un apagón largo basta con un disparo
    assert cron_due("0 3 * * *", t(10, 0, day=1), t(10, 0, day=20))
