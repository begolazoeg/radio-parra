"""
Tests unitarios del Playout: unidades del scheduler, estados y registro en play_log.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import GridConfig
from radio.core.models import Segment
from radio.core.playout import Playout
from radio.core.store import DB
from radio.producers.time_signal import hour_tag
from radio.providers.audio.null import NullAudioBackend
from radio.sim import SimAudioBackend

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


class Station:
    """Montaje mínimo: BD en memoria, reloj falso y audio que avanza el reloj."""

    def __init__(self, tmp_path: Path, now: datetime = T0, *, verify_files: bool = True,
                 emergency_dir: Path | None = None, sim_audio: bool = True,
                 grid_config: GridConfig | None = None) -> None:
        self.tmp = tmp_path
        self.db = DB(":memory:")
        self.clock = FakeClock(now)
        self.null_audio = NullAudioBackend()
        self.sim_audio = SimAudioBackend(self.clock, self._duration)
        self.playout = Playout(
            self.db,
            grid_config or grid(),
            self.sim_audio if sim_audio else self.null_audio,
            self.clock,
            rng=random.Random(0),
            verify_files=verify_files,
            emergency_dir=emergency_dir,
        )
        self._n = 0

    def _duration(self, path: Path) -> float:
        seg = self.db.find_by_path(path)
        return seg.duration_s if seg else 0.0

    def add(self, seg_id: str, kind: str = "music", *, duration: float = 200.0,
            tags: list[str] | None = None, exists: bool = True,
            expires_at: datetime | None = None, parent_id: str | None = None,
            priority: int = 0) -> Path:
        path = self.tmp / f"{seg_id}.mp3"
        if exists:
            path.write_bytes(b"x")
        self._n += 1
        self.db.add_segment(Segment(
            id=seg_id, kind=kind, factual=kind != "music", path=path, duration_s=duration,
            created_at=datetime(2026, 1, 1, 0, 0, self._n, tzinfo=MADRID), producer="test",
            expires_at=expires_at, parent_id=parent_id, priority=priority,
            meta={"title": seg_id, "tags": tags or []},
        ))
        return path

    def signal(self, seg_id: str, hour: datetime, **kw: Any) -> Path:
        return self.add(seg_id, "time_signal", duration=3.0, tags=[hour_tag(hour)],
                        priority=1, **kw)

    def status(self, seg_id: str) -> str:
        seg = self.db.get_segment(seg_id)
        assert seg is not None
        return seg.status


# ── Música ────────────────────────────────────────────────────────────────────

def test_music_play_logged_with_timestamps_and_stays_ready(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("m1", duration=180.0, tags=["artist:a"])

    outcome = st.playout.step()

    assert outcome is not None
    assert (outcome.segment_id, outcome.kind, outcome.started_at) == ("m1", "music", T0)
    assert outcome.duration_s == 180.0 and "música" in outcome.reason
    assert outcome.rung == 1
    plays = st.db.list_play_log()
    assert len(plays) == 1
    assert plays[0].started_at == T0
    assert plays[0].ended_at == T0 + timedelta(seconds=180)
    assert plays[0].skipped is False
    assert (plays[0].segment_id, plays[0].kind, plays[0].mode) == ("m1", "music", "default")
    assert st.status("m1") == "ready"


def test_mode_is_logged(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.playout.mode = "tinydesk"   # no existe en esta parrilla → se usa "default"
    st.add("m1")
    st.playout.step()
    assert st.db.list_play_log()[0].mode == "tinydesk"


def test_music_alternates_artists(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("a1", tags=["artist:a"])
    st.add("a2", tags=["artist:a"])
    st.add("b1", tags=["artist:b"])
    outcomes = [st.playout.step() for _ in range(4)]
    artists = [o.segment_id[0] for o in outcomes if o]
    assert len(artists) == 4
    assert all(x != y for x, y in zip(artists, artists[1:], strict=False))


def test_music_falls_back_when_only_same_artist(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("a1", tags=["artist:a"])
    st.add("a2", tags=["artist:a"])
    first, second = st.playout.step(), st.playout.step()
    assert first is not None and second is not None
    assert {first.segment_id, second.segment_id} == {"a1", "a2"}
    assert second.rung == 3 and "mismo artista" in second.reason


def test_music_avoids_crossing_time_signal_window(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 50, tzinfo=MADRID))
    st.add("long", duration=900.0)   # acabaría a las 11:05 → pisaría la señal
    st.add("short", duration=200.0)
    st.signal("ts11", datetime(2026, 1, 5, 11, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "short"


def test_music_deadline_ignored_without_next_signal(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 50, tzinfo=MADRID))
    st.add("long", duration=900.0)
    st.add("short", duration=200.0)
    ids = {o.segment_id for o in (st.playout.step(), st.playout.step()) if o}
    assert ids == {"long", "short"}


# ── Señal horaria ─────────────────────────────────────────────────────────────

def test_time_signal_matches_current_local_hour(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    st.add("m1")
    st.signal("ts11", datetime(2026, 1, 5, 11, tzinfo=MADRID))
    st.signal("ts10", datetime(2026, 1, 5, 10, tzinfo=MADRID))

    outcome = st.playout.step()

    assert outcome is not None and outcome.segment_id == "ts10"
    assert outcome.unit.interrupt is True
    assert st.status("ts10") == "retired"
    assert st.status("ts11") == "ready"


def test_time_signal_too_late_is_skipped(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 2, tzinfo=MADRID))   # 120 s > 90 s
    st.add("m1")
    st.signal("ts10", datetime(2026, 1, 5, 10, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "m1"


def test_expired_time_signal_is_not_aired(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    st.add("m1")
    st.signal("ts10", datetime(2026, 1, 5, 10, tzinfo=MADRID),
              expires_at=datetime(2026, 1, 5, 10, 0, 30, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "m1"
    assert st.status("ts10") == "ready"   # caducarla es cosa del productor


def test_time_signal_matches_local_hour_from_utc_clock(tmp_path: Path) -> None:
    # 09:01 UTC = 10:01 en Madrid (invierno)
    st = Station(tmp_path, datetime(2026, 1, 5, 9, 1, tzinfo=ZoneInfo("UTC")))
    st.add("m1")
    st.signal("ts10", datetime(2026, 1, 5, 10, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "ts10"


def test_time_signal_without_match_falls_back_to_music(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    st.add("m1")
    st.signal("ts11", datetime(2026, 1, 5, 11, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "m1"
    assert st.status("ts11") == "ready"


def test_signal_already_aired_this_hour_across_timezones(tmp_path: Path) -> None:
    # Una señal emitida (registrada con reloj UTC) cuenta para la hora local en curso
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    st.add("m1")
    st.add("old", "time_signal", duration=3.0)
    pid = st.db.log_play_start("old", "time_signal", "default",
                               datetime(2026, 1, 5, 9, 0, 5, tzinfo=ZoneInfo("UTC")))
    st.db.log_play_end(pid, datetime(2026, 1, 5, 9, 0, 8, tzinfo=ZoneInfo("UTC")))
    st.signal("ts10", datetime(2026, 1, 5, 10, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "m1"


@pytest.mark.parametrize("window", [0, 120])
def test_history_window_limits_records(tmp_path: Path, window: int) -> None:
    # Con ventana 0 la señal emitida hace 30 s no cuenta y se repite la interrupción
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    st.playout.history_window = timedelta(minutes=window)
    st.add("m1")
    st.add("old", "time_signal", duration=3.0)
    st.db.log_play_start("old", "time_signal", "default",
                         datetime(2026, 1, 5, 10, 0, 30, tzinfo=MADRID))
    st.signal("ts10", datetime(2026, 1, 5, 10, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None
    assert outcome.segment_id == ("ts10" if window == 0 else "m1")


# ── Unidades, patrón y vinculación ────────────────────────────────────────────

def test_linked_host_intro_airs_before_its_music(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("m1", duration=180.0)
    st.add("intro", "host_intro", duration=15.0, parent_id="m1")

    outcome = st.playout.step()

    assert outcome is not None
    assert [a.segment_id for a in outcome.aired] == ["intro", "m1"]
    assert outcome.segment_id == "m1" and outcome.started_at == T0
    assert outcome.duration_s == 195.0
    plays = st.db.list_play_log()
    assert [p.segment_id for p in plays] == ["intro", "m1"]
    assert plays[1].started_at == T0 + timedelta(seconds=15)
    assert st.status("intro") == "retired" and st.status("m1") == "ready"


def test_pattern_cycles_across_steps(tmp_path: Path) -> None:
    st = Station(tmp_path, grid_config=grid(["music", "jingle"], signal=False))
    st.add("m1", tags=["artist:a"])
    st.add("m2", tags=["artist:b"])
    st.add("j1", "jingle", duration=8.0)
    kinds = [o.kind for o in (st.playout.step() for _ in range(4)) if o]
    assert kinds == ["music", "jingle", "music", "jingle"]


# ── Estados y errores ─────────────────────────────────────────────────────────

def test_missing_file_quarantines_and_retries(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("gone", exists=False)
    st.add("ok")

    outcomes = [st.playout.step(), st.playout.step()]   # tarde o temprano elige "gone"

    assert all(o is not None and o.segment_id == "ok" for o in outcomes)
    assert st.status("gone") == "quarantined"
    assert [p.segment_id for p in st.db.list_play_log()] == ["ok", "ok"]


def test_missing_file_ignored_without_verification(tmp_path: Path) -> None:
    st = Station(tmp_path, verify_files=False)
    st.add("gone", exists=False)
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "gone"


def test_all_files_missing_returns_none(tmp_path: Path) -> None:
    st = Station(tmp_path)
    for i in range(3):
        st.add(f"gone{i}", exists=False)
    assert st.playout.step() is None
    assert all(st.status(f"gone{i}") == "quarantined" for i in range(3))
    assert st.playout.last_unit is not None and st.playout.last_unit.rung == 5


def test_talk_retired_after_airing(tmp_path: Path) -> None:
    st = Station(tmp_path, grid_config=grid(["talk"], {"weather": 1}))
    st.add("m1")
    st.add("w1", "weather", duration=15.0)
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "w1"
    assert st.status("w1") == "retired"


def test_jingle_stays_ready(tmp_path: Path) -> None:
    st = Station(tmp_path, grid_config=grid(["jingle"]))
    st.add("j1", "jingle", duration=8.0)
    outcome = st.playout.step()
    assert outcome is not None and outcome.kind == "jingle"
    assert st.status("j1") == "ready"


def test_interrupted_talk_stays_ready(tmp_path: Path) -> None:
    st = Station(tmp_path, grid_config=grid(["talk"], {"weather": 1}))
    st.add("w1", "weather", duration=15.0)

    class InterruptingAudio(NullAudioBackend):
        def play(self, path: Path) -> None:
            super().play(path)
            st.playout.interrupt()

    st.playout.audio = InterruptingAudio()
    outcome = st.playout.step()

    assert outcome is not None and outcome.aired[0].skipped
    assert st.status("w1") == "ready"
    assert st.db.list_play_log()[0].skipped is True


def test_interrupted_unit_stops_remaining_segments(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("m1", duration=180.0)
    st.add("intro", "host_intro", duration=15.0, parent_id="m1")

    class InterruptingAudio(NullAudioBackend):
        def play(self, path: Path) -> None:
            super().play(path)
            st.playout.interrupt()

    st.playout.audio = InterruptingAudio()
    outcome = st.playout.step()
    assert outcome is not None and [a.segment_id for a in outcome.aired] == ["intro"]
    assert st.status("intro") == "ready"


# ── Emergencia ────────────────────────────────────────────────────────────────

def test_nothing_to_play_without_emergency(tmp_path: Path) -> None:
    st = Station(tmp_path, sim_audio=False)
    assert st.playout.step() is None
    assert st.null_audio.calls == []
    assert st.playout.last_emergency is None
    assert st.playout.last_unit is not None and st.playout.last_unit.is_emergency


def test_emergency_audio_rotates_and_is_not_logged(tmp_path: Path) -> None:
    emergency = tmp_path / "emergency"
    emergency.mkdir()
    for name in ("b.mp3", "a.ogg", ".gitkeep", "notes.txt"):
        (emergency / name).write_bytes(b"x")
    st = Station(tmp_path, emergency_dir=emergency, sim_audio=False)

    assert st.playout.step() is None
    assert st.playout.last_emergency == emergency / "a.ogg"
    assert st.playout.step() is None
    assert st.playout.last_emergency == emergency / "b.mp3"
    assert [c["path"].name for c in st.null_audio.calls] == ["a.ogg", "b.mp3"]
    assert st.db.list_play_log() == []
