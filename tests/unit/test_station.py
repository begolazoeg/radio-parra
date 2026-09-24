"""
Tests del bucle de la emisora (sin mpv ni red).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import ProducersConfig, ProducerSettings, ProviderSettings, RadioConfig
from radio.core.playout import Playout
from radio.core.scheduler import Scheduler
from radio.core.store import DB
from radio.providers.audio.null import NullAudioBackend
from radio.station import build_runner, run_station_loop

REPO = Path(__file__).parents[2]
MADRID = ZoneInfo("Europe/Madrid")


def config(llm: str = "fake", tts: str = "fake") -> RadioConfig:
    base = RadioConfig.load(REPO / "config")
    station = base.station.model_copy(update={"providers": {
        "llm": ProviderSettings(name=llm), "tts": ProviderSettings(name=tts),
    }})
    producers = ProducersConfig(producers={
        "time_signal": ProducerSettings(active=True, interval_minutes=30),
    })
    return base.model_copy(update={"station": station, "producers": producers})


def stop_after(n: int) -> Callable[[], bool]:
    """should_stop que deja pasar n comprobaciones y luego pide parar."""
    calls = [0]

    def should_stop() -> bool:
        calls[0] += 1
        return calls[0] > n

    return should_stop


def make_playout(db: DB, clock: FakeClock, emergency_dir: Path | None = None) -> Playout:
    return Playout(
        db, Scheduler(RadioConfig.load(REPO / "config").grid, rng=random.Random(0)),
        NullAudioBackend(), clock, emergency_dir=emergency_dir,
    )


def test_station_loop_airs_and_runs_producers(tmp_path: Path) -> None:
    db = DB(":memory:")
    clock = FakeClock(datetime(2026, 1, 5, 10, 20, tzinfo=MADRID))
    for i in range(3):
        path = tmp_path / f"m{i}.mp3"
        path.write_bytes(b"x")
        db.add_segment(id=f"m{i}", kind="music", status="ready", title=f"m{i}",
                       duration_s=200, audio_path=path, producer="t", tags=[f"artist:{i}"])
    playout = make_playout(db, clock)
    runner = build_runner(config(), db, clock, tmp_path / "data", REPO / "prompts")
    assert runner is not None
    sleeps: list[float] = []

    aired = run_station_loop(playout, runner, should_stop=lambda: len(db.list_plays()) >= 3,
                             sleep=sleeps.append)

    assert aired == 3
    assert [p["segment_id"] for p in db.list_plays()] == ["m0", "m1", "m2"]
    assert db.count_ready_by_kind()["time_signal"] == 2   # el producer ha corrido
    assert sleeps == []


def test_station_loop_sleeps_when_nothing_to_play(tmp_path: Path) -> None:
    db = DB(":memory:")
    playout = make_playout(db, FakeClock(datetime(2026, 1, 5, tzinfo=MADRID)))
    sleeps: list[float] = []
    aired = run_station_loop(playout, None, should_stop=stop_after(4), sleep=sleeps.append)
    assert aired == 0 and sleeps == [5.0] * 4


def test_station_loop_no_sleep_after_emergency(tmp_path: Path) -> None:
    emergency = tmp_path / "emergency"
    emergency.mkdir()
    (emergency / "e.mp3").write_bytes(b"x")
    playout = make_playout(DB(":memory:"),
                           FakeClock(datetime(2026, 1, 5, tzinfo=MADRID)), emergency_dir=emergency)
    sleeps: list[float] = []
    run_station_loop(playout, None, should_stop=stop_after(2), sleep=sleeps.append)
    assert sleeps == []


def test_station_loop_stops_immediately() -> None:
    playout = make_playout(DB(":memory:"), FakeClock(datetime(2026, 1, 5, tzinfo=MADRID)))
    assert run_station_loop(playout, None, should_stop=lambda: True, sleep=lambda _s: None) == 0


def test_build_runner_music_only_when_provider_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock(datetime(2026, 1, 5, tzinfo=MADRID))
    assert build_runner(config(llm="anthropic"), DB(":memory:"), clock, tmp_path, tmp_path) is None
    assert "solo música" in caplog.text
