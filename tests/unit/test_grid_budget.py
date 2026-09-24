"""
Tests del presupuesto de charla (radio.grid.budget).
"""

from __future__ import annotations

from datetime import timedelta

from radio.grid.budget import fits_budget, is_talk, talk_ratio
from tests.fixtures.grid import entry, local, seg

WINDOW = timedelta(minutes=60)
T = local(2026, 1, 5, 12)


def test_talk_kinds() -> None:
    assert not is_talk("music") and not is_talk("jingle") and not is_talk("stinger")
    assert is_talk("time_signal") and is_talk("host_intro") and is_talk("kind_nuevo")


def test_ratio_clips_to_window_and_uses_full_window_denominator() -> None:
    w = seg("w", "weather", 600.0)
    m = seg("m", "music", 3000.0)
    old = seg("old", "weather", 600.0)
    history = [
        entry(old, T - timedelta(minutes=65)),     # 5 min dentro de la ventana
        entry(w, T - timedelta(minutes=55)),
        entry(m, T - timedelta(minutes=45)),
    ]
    assert abs(talk_ratio(history, T, WINDOW) - (300 + 600) / 3600) < 1e-9
    assert talk_ratio([], T, WINDOW) == 0.0


def test_fits_budget_projects_each_talk_segment() -> None:
    history = [entry(seg("w", "weather", 700.0), T - timedelta(minutes=30))]
    # 700 s ya emitidos; tope 0.22 * 3600 = 792 s
    assert fits_budget(history, [("ephemeris", 90.0)], T, window=WINDOW, max_ratio=0.22)
    assert not fits_budget(history, [("ephemeris", 93.0)], T, window=WINDOW, max_ratio=0.22)
    # La reserva para interrupciones reduce el margen
    assert not fits_budget(history, [("ephemeris", 90.0)], T, window=WINDOW, max_ratio=0.22,
                           reserve_s=5.0)
    # La música nunca consume presupuesto
    assert fits_budget(history, [("music", 5000.0)], T, window=WINDOW, max_ratio=0.0)
    # [host_intro, music]: cuenta la intro
    assert not fits_budget(history, [("host_intro", 100.0), ("music", 200.0)], T,
                           window=WINDOW, max_ratio=0.22)


def test_fits_budget_accounts_for_talk_leaving_the_window() -> None:
    # La palabra de hace 59 min sale de la ventana mientras suena la nueva
    history = [entry(seg("w", "weather", 780.0), T - timedelta(minutes=72))]
    assert fits_budget(history, [("ephemeris", 700.0)], T, window=WINDOW, max_ratio=0.22)
