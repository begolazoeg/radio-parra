"""
Integración pura de la parrilla (§9): 48 h de emisión con stock sintético sobre
``next_unit`` + ``advance_state``, sin BD. Debe terminar sin huecos y cumpliendo
todas las propiedades del scheduler con la parrilla real (config/grid.yaml).
"""

from __future__ import annotations

from datetime import UTC, timedelta
from pathlib import Path

import pytest

from radio.core.config import RadioConfig
from radio.core.models import Segment
from tests.fixtures.grid import (
    MADRID,
    fiction_after_factual,
    local,
    max_talk_ratio,
    music_catalog,
    run_pure,
    seg,
)

REPO = Path(__file__).parents[2]


def synthetic_stock() -> list[Segment]:
    music = music_catalog(250, artists=40, seed=11)
    intros = [seg(f"intro-{m.id}", "host_intro", 18.0, parent_id=m.id) for m in music[::4]]
    talk = [
        seg(f"{kind}-{i}", kind, 45.0 + (i * 7) % 45)
        for kind in ("weather", "ephemeris", "horoscope", "word_of_day", "consultorio",
                     "liga", "artist_fact", "trivia", "radionovela", "interview")
        for i in range(40)
    ]
    jingles = [seg(f"j{i}", "jingle", 7.0 + i) for i in range(3)]
    return music + intros + talk + jingles


@pytest.mark.parametrize("seed", [1, 2])
def test_48h_pure_run_has_no_gaps(seed: int) -> None:
    grid = RadioConfig.load(REPO / "config").grid
    start = local(2026, 3, 28, 0)          # cruza el cambio de hora del 29-03
    run = run_pure(grid, synthetic_stock(), start, 48, seed)

    assert run.emergency_s == 0
    assert all(u.rung < 5 for _, u in run.units)
    # Sin huecos: cada segmento empieza cuando acaba el anterior
    for prev, cur in zip(run.aired, run.aired[1:], strict=False):
        assert cur.start == prev.end
    assert run.aired[0].start == start and run.aired[-1].end >= start + timedelta(hours=48)

    # Señal horaria en todas las horas (menos la primera), dentro de max_late_seconds
    signals = [a for a in run.aired if a.seg.kind == "time_signal"]
    assert len(signals) >= 47
    for a in signals:
        local_start = a.start.astimezone(MADRID)
        top = local_start.replace(minute=0, second=0, microsecond=0)
        assert (a.start.astimezone(UTC) - top.astimezone(UTC)).total_seconds() <= 90
        assert f"hour:{top:%Y-%m-%dT%H}" in a.seg.tags

    assert max_talk_ratio(run, start) <= 0.22 + 1e-9
    assert fiction_after_factual(run) == 0
    kinds = {a.seg.kind for a in run.aired}
    assert {"music", "host_intro", "jingle", "consultorio", "weather"} <= kinds
    # Nunca dos canciones seguidas del mismo artista
    music = [a.seg for a in run.aired if a.seg.kind == "music"]
    assert all(set(x.tags).isdisjoint(y.tags) or not x.tags
               for x, y in zip(music, music[1:], strict=False))
