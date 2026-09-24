"""
Tests del motor de la emisora (§4.4) con FakeClock + FakeEventBackend: sin hilos,
sin mpv y sin red.
"""

from __future__ import annotations

import random
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import GridConfig, RadioConfig
from radio.core.models import Segment
from radio.core.store import DB
from radio.grid.rules import hour_tag
from radio.providers.audio.fake import FakeEventBackend
from radio.sim import drive
from radio.station import AiredItem, StationEngine
from radio.station import engine as engine_module
from radio.station.engine import audio_duration

REPO = Path(__file__).parents[2]
MADRID = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 1, 5, 10, 20, tzinfo=MADRID)


def grid(pattern: list[str] | None = None, pool: dict[str, float] | None = None,
         signal: bool = True) -> GridConfig:
    mode: dict[str, Any] = {
        "dayparts": [{"name": "todo", "from": "00:00", "to": "24:00",
                      "pattern": pattern or ["music"], "talk_pool": pool or {}}],
    }
    if signal:
        mode["interrupts"] = [{"kind": "time_signal", "when": "minute == 0",
                               "max_late_seconds": 90}]
    return GridConfig.model_validate({"timezone": "Europe/Madrid", "modes": {"default": mode}})


def make_wav(path: Path, seconds: float = 0.05) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(seconds * 8000))
    return path


class Rig:
    """Emisora de prueba: BD en memoria, reloj falso y backend con eventos síncronos."""

    def __init__(self, tmp_path: Path, now: datetime = T0, *, grid_config: GridConfig | None = None,
                 emergency: bool = True, **engine_kw: Any) -> None:
        self.tmp = tmp_path
        self.db = DB(":memory:")
        self.clock = FakeClock(now)
        self.backend = FakeEventBackend(self.clock, duration_of=self._duration,
                                        advance=self.clock.advance)
        self.aired: list[AiredItem] = []
        emergency_dir = tmp_path / "emergency"
        if emergency:
            emergency_dir.mkdir(exist_ok=True)
            make_wav(emergency_dir / "loop.wav", 0.5)
        engine_kw.setdefault("rng", random.Random(0))
        self.engine = StationEngine(
            self.db, grid_config or grid(), self.backend, self.clock,
            emergency_dir=emergency_dir, on_aired=self.aired.append, **engine_kw,
        )
        self._n = 0

    def _duration(self, path: Path) -> float:
        seg = self.db.find_by_path(path)
        return seg.duration_s if seg else audio_duration(path)

    def add(self, seg_id: str, kind: str = "music", *, duration: float = 200.0,
            tags: list[str] | None = None, exists: bool = True, priority: int = 0,
            expires_at: datetime | None = None, factual: bool | None = None) -> Path:
        path = self.tmp / f"{seg_id}.wav"
        if exists:
            make_wav(path)
        self._n += 1
        self.db.add_segment(Segment(
            id=seg_id, kind=kind, factual=kind != "music" if factual is None else factual,
            path=path, duration_s=duration,
            created_at=datetime(2026, 1, 1, 0, 0, self._n, tzinfo=MADRID), producer="test",
            priority=priority, expires_at=expires_at, meta={"title": seg_id, "tags": tags or []},
        ))
        return path

    def signal(self, seg_id: str, hour: datetime) -> Path:
        return self.add(seg_id, "time_signal", duration=3.0, tags=[hour_tag(hour)], priority=1,
                        expires_at=hour + timedelta(minutes=5))

    def status(self, seg_id: str) -> str:
        seg = self.db.get_segment(seg_id)
        assert seg is not None
        return seg.status

    def run_until(self, t: datetime) -> float:
        return drive(self.engine, self.backend, self.clock, t)

    def log(self) -> list[tuple[str | None, str, bool]]:
        return [(p.segment_id, p.kind, p.skipped) for p in self.db.list_play_log()]


def at(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 1, 5, h, m, s, tzinfo=MADRID)


# ── Cola, lookahead y play_log ────────────────────────────────────────────────

def test_start_fills_lookahead_and_logs_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    for i in range(5):
        rig.add(f"m{i}", tags=[f"artist:{i}"])
    rig.engine.start()
    assert rig.backend.current() is not None
    assert rig.backend.queued() == 2 and rig.engine.queue.pending_units() == 2
    [entry] = rig.db.list_play_log()
    assert entry.started_at == T0 and entry.ended_at is None and entry.mode == "default"


def test_play_log_written_at_start_and_end(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.add("a", duration=180.0, tags=["artist:a"])
    rig.add("b", duration=120.0, tags=["artist:b"])
    rig.engine.start()
    rig.backend.finish()
    first, second = rig.db.list_play_log()[:2]
    assert first.ended_at == T0 + timedelta(seconds=first.duration_s or 0)
    assert first.skipped is False and second.started_at == first.ended_at
    assert rig.status("a") == rig.status("b") == "ready"      # la música sigue en rotación
    assert rig.backend.queued() == 2                             # se ha rellenado


def test_lookahead_accounts_for_queued_units(tmp_path: Path) -> None:
    """El scheduler ve lo que ya está en cola: nunca el mismo artista seguido."""
    rig = Rig(tmp_path, grid_config=grid(signal=False))
    rig.add("a1", tags=["artist:a"])
    rig.add("a2", tags=["artist:a"])
    rig.add("b1", tags=["artist:b"])
    rig.add("c1", tags=["artist:c"])
    rig.engine.start()
    rig.run_until(T0 + timedelta(hours=2))
    music = [rig.db.get_segment(sid) for sid, kind, _ in rig.log() if kind == "music" and sid]
    artists = [next(t for t in s.tags if t.startswith("artist:")) for s in music if s]
    assert len(artists) > 20
    assert all(x != y for x, y in zip(artists, artists[1:], strict=False))


def test_queued_talk_is_not_picked_twice_and_retires_after_eof(tmp_path: Path) -> None:
    rig = Rig(tmp_path, grid_config=grid(["music", "talk"], {"weather": 1}, signal=False))
    rig.add("m1", tags=["artist:a"])
    rig.add("m2", tags=["artist:b"])
    rig.add("w1", "weather", duration=30.0)
    rig.add("w2", "weather", duration=30.0)
    rig.engine.start()
    queued_talk = [i.segment.id for i in rig.engine.queue.items()
                   if i.segment and i.kind == "weather"]
    assert queued_talk and len(set(queued_talk)) == len(queued_talk)
    rig.run_until(T0 + timedelta(hours=1))
    talk = [sid for sid, kind, _ in rig.log() if kind == "weather"]
    assert sorted(talk) == ["w1", "w2"]                         # cada una, una sola vez
    assert rig.status("w1") == rig.status("w2") == "retired"


def test_pattern_cycles_through_the_queue(tmp_path: Path) -> None:
    rig = Rig(tmp_path, grid_config=grid(["music", "jingle"], signal=False))
    rig.add("m1", tags=["artist:a"])
    rig.add("m2", tags=["artist:b"])
    rig.add("j1", "jingle", duration=8.0)
    rig.engine.start()
    rig.run_until(T0 + timedelta(minutes=30))
    kinds = [kind for _, kind, _ in rig.log()][:6]
    assert kinds == ["music", "jingle"] * 3


# ── Audios que faltan ─────────────────────────────────────────────────────────

def test_missing_file_at_enqueue_is_quarantined(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.add("gone", exists=False)
    rig.add("ok")
    rig.engine.start()
    rig.run_until(T0 + timedelta(minutes=20))
    assert rig.status("gone") == "quarantined"
    assert {sid for sid, _, _ in rig.log()} == {"ok"}
    assert rig.engine.stats.quarantined == 1


def test_file_deleted_while_queued_is_quarantined_and_queue_refilled(tmp_path: Path) -> None:
    """La caché LRU de Tiny Desk puede borrar un archivo que ya estaba en cola."""
    rig = Rig(tmp_path, grid_config=grid(signal=False))
    for i in range(4):
        rig.add(f"m{i}", tags=[f"artist:{i}"])
    rig.engine.start()
    current = rig.engine.queue.current
    assert current is not None and current.segment is not None
    current.path.unlink()
    rig.backend.finish("error")          # mpv no puede leerlo
    assert rig.status(current.segment.id) == "quarantined"
    assert rig.log()[0] == (current.segment.id, "music", True)   # cerrado como saltado
    assert rig.backend.current() is not None and rig.backend.queued() == 2


def test_error_mid_play_with_file_present_keeps_segment(tmp_path: Path) -> None:
    rig = Rig(tmp_path, grid_config=grid(signal=False))
    rig.add("a", tags=["artist:a"])
    rig.add("b", tags=["artist:b"])
    rig.engine.start()
    first = rig.engine.queue.current
    assert first is not None and first.segment is not None
    rig.backend.crash()                   # mpv muere; el watchdog lo relanza
    rig.engine.tick()
    assert rig.status(first.segment.id) == "ready"
    assert rig.engine.stats.restarts == 1
    assert rig.backend.current() is not None and rig.backend.queued() == 2


# ── Interrupciones ────────────────────────────────────────────────────────────

def test_interrupt_cuts_music_on_the_hour(tmp_path: Path) -> None:
    rig = Rig(tmp_path, at(10, 50))
    rig.add("long", duration=1500.0, tags=["artist:a"])      # acabaría a las 11:15
    rig.add("long2", duration=1500.0, tags=["artist:b"])
    rig.signal("ts11", at(11))
    rig.engine.start()
    assert rig.engine.interrupt_at == at(11)
    rig.run_until(at(11, 10))

    plays = rig.db.list_play_log()
    signal = next(p for p in plays if p.kind == "time_signal")
    assert signal.started_at == at(11)                         # puntual al segundo
    cut = plays[0]
    assert cut.skipped is True and cut.ended_at == at(11)
    assert rig.engine.stats.interrupts == 1 and rig.engine.stats.music_cuts == 1
    assert rig.status("ts11") == "retired"
    assert rig.status(cut.segment_id or "") == "ready"
    assert any(a.cut and a.kind == "music" for a in rig.aired)
    assert rig.backend.current() is not None                   # sigue la música
    assert rig.engine.interrupt_at == at(12)


def test_interrupt_already_queued_on_time_is_left_alone(tmp_path: Path) -> None:
    rig = Rig(tmp_path, at(10, 56))
    rig.add("m", duration=270.0, tags=["artist:a"])            # acaba 11:00:30
    rig.add("m2", duration=200.0, tags=["artist:b"])
    rig.signal("ts11", at(11))
    rig.engine.start()
    rig.run_until(at(11, 5))
    signal = next(p for p in rig.db.list_play_log() if p.kind == "time_signal")
    assert signal.started_at == at(11, 0, 30)
    assert rig.engine.stats.interrupts == 0 and rig.engine.stats.music_cuts == 0
    assert not any(c["action"] == "clear_pending" for c in rig.backend.calls)


def test_interrupt_preempts_queue_without_cutting_when_music_ends_in_time(tmp_path: Path) -> None:
    rig = Rig(tmp_path, at(10, 56), cut_music=False)
    rig.add("m", duration=270.0, tags=["artist:a"])            # acaba 11:00:30
    rig.engine.start()
    assert rig.backend.queued() == 2                           # m otra vez (peldaño 3)
    rig.signal("ts11", at(11))                                 # llega tarde al stock
    rig.run_until(at(11, 5))
    plays = rig.db.list_play_log()
    assert plays[0].skipped is False                           # no se cortó
    assert plays[1].kind == "time_signal" and plays[1].started_at == at(11, 0, 30)
    assert rig.engine.stats.interrupts == 1 and rig.engine.stats.music_cuts == 0
    assert any(c["action"] == "clear_pending" for c in rig.backend.calls)


def test_cut_music_false_omits_late_interrupt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, at(10, 50), cut_music=False)
    rig.add("long", duration=1500.0, tags=["artist:a"])
    rig.add("long2", duration=1500.0, tags=["artist:b"])
    rig.signal("ts11", at(11))
    rig.engine.start()
    rig.run_until(at(11, 30))
    assert all(p.kind == "music" and not p.skipped for p in rig.db.list_play_log()[:-1])
    assert rig.engine.stats.interrupts_omitted == 1 and rig.engine.stats.music_cuts == 0
    assert rig.status("ts11") == "ready"


def test_interrupt_cuts_talk_that_would_make_it_late(tmp_path: Path) -> None:
    rig = Rig(tmp_path, at(10, 59, 30), grid_config=grid(["talk"], {"weather": 1}))
    rig.add("w", "weather", duration=200.0)                   # acabaría 11:02:50
    rig.add("m", tags=["artist:a"])
    rig.signal("ts11", at(11))
    rig.engine.start()
    rig.run_until(at(11, 5))
    plays = rig.db.list_play_log()
    assert (plays[0].segment_id, plays[0].skipped) == ("w", True)
    assert plays[1].kind == "time_signal" and plays[1].started_at == at(11)
    assert rig.status("w") == "ready"                          # cortada: no se retira


# ── Emergencia (§8) y nunca silencio (inv. 3) ─────────────────────────────────

def test_emergency_loop_when_nothing_to_play_then_recovers(tmp_path: Path) -> None:
    rig = Rig(tmp_path, emergency_retry_s=30.0)
    rig.engine.start()
    [entry] = rig.db.list_play_log()
    assert (entry.segment_id, entry.kind) == (None, "emergency")
    assert rig.engine.retry_at == T0 + timedelta(seconds=30)
    silence = rig.run_until(T0 + timedelta(seconds=20))
    assert silence == 0
    assert rig.engine.stats.emergencies > 1                     # el bucle da vueltas
    rig.add("m", tags=["artist:a"])                             # llega stock (radio produce)
    silence += rig.run_until(T0 + timedelta(minutes=2))
    assert silence == 0
    last = rig.db.list_play_log()
    assert last[-1].segment_id == "m"
    assert rig.engine.stats.units_started[5] >= 1


def test_scheduler_failure_falls_back_to_emergency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("fallo del scheduler")

    monkeypatch.setattr(engine_module, "next_unit", broken)
    rig = Rig(tmp_path)
    rig.add("m")
    rig.engine.start()
    assert rig.log() == [(None, "emergency", False)]


def test_without_emergency_audio_it_logs_but_does_not_crash(tmp_path: Path) -> None:
    rig = Rig(tmp_path, emergency=False)
    rig.engine.start()
    assert rig.backend.current() is None and rig.db.list_play_log() == []


# ── Modo tinydesk: solo música, en bucle ─────────────────────────────────────

def test_music_only_stock_loops_for_hours(tmp_path: Path) -> None:
    config = RadioConfig.load(REPO / "config")
    rig = Rig(tmp_path, at(0), grid_config=config.grid, mode="tinydesk")
    for i in range(3):
        rig.add(f"td{i}", duration=1200.0, tags=[f"artist:{i}"])
    rig.engine.start()
    silence = rig.run_until(at(0) + timedelta(hours=10))
    plays = rig.db.list_play_log()
    assert silence == 0
    assert {p.kind for p in plays} == {"music"}
    assert len(plays) >= 30                                     # 3 conciertos, una y otra vez
    assert {p.mode for p in plays} == {"tinydesk"}


# ── Parada y determinismo ─────────────────────────────────────────────────────

def test_stop_prevents_planning_and_close_logs_last_item(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.add("a", tags=["artist:a"])
    rig.add("b", tags=["artist:b"])
    rig.engine.start()
    rig.engine.stop()
    assert rig.engine.next_wakeup() is None
    rig.backend.close()
    rig.engine.drain()
    [entry] = rig.db.list_play_log()
    assert entry.skipped is True and entry.ended_at == T0


def test_same_seed_same_play_log(tmp_path: Path) -> None:
    def run(sub: str) -> list[tuple[str | None, str, bool]]:
        d = tmp_path / sub
        d.mkdir()
        rig = Rig(d, grid_config=grid(["music", "talk", "jingle"], {"weather": 1}))
        for i in range(12):
            rig.add(f"m{i}", duration=150.0 + 20 * i, tags=[f"artist:{i % 5}"])
        for i in range(6):
            rig.add(f"w{i}", "weather", duration=40.0)
        rig.add("j", "jingle", duration=8.0)
        rig.signal("ts11", at(11))
        rig.engine.start()
        rig.run_until(at(12))
        return rig.log()

    assert run("a") == run("b")


def test_from_config_reads_station_yaml(tmp_path: Path) -> None:
    config = RadioConfig.load(REPO / "config")
    backend = FakeEventBackend(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
    engine = StationEngine.from_config(config, DB(":memory:"), backend,
                                       FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
    assert engine.lookahead_units == config.station.playout.lookahead_units == 2
    assert engine.cut_music is True
    assert engine.emergency_dir is not None
    assert (engine.emergency_dir / "emergency_loop.wav").is_file()
