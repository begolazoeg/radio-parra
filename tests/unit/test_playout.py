"""
Tests unitarios del Playout: selección de segmentos, estados y registro en play_log.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import GridConfig, TimeSlot
from radio.core.models import Segment
from radio.core.playout import Playout
from radio.core.scheduler import Scheduler
from radio.core.store import DB
from radio.producers.time_signal import hour_tag
from radio.providers.audio.null import NullAudioBackend
from radio.sim import SimAudioBackend

MADRID = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 1, 5, 10, 20, tzinfo=MADRID)


def grid() -> GridConfig:
    return GridConfig(
        timezone="Europe/Madrid",
        slots=[TimeSlot(name="todo", start="00:00", end="00:00", music_ratio=0.7)],
    )


class Station:
    """Montaje mínimo: BD en memoria, reloj falso y audio que avanza el reloj."""

    def __init__(self, tmp_path: Path, now: datetime = T0, *, verify_files: bool = True,
                 emergency_dir: Path | None = None, sim_audio: bool = True) -> None:
        self.tmp = tmp_path
        self.db = DB(":memory:")
        self.clock = FakeClock(now)
        self.null_audio = NullAudioBackend()
        self.sim_audio = SimAudioBackend(self.clock, self._duration)
        self.playout = Playout(
            self.db,
            Scheduler(grid(), rng=random.Random(0)),
            self.sim_audio if sim_audio else self.null_audio,
            self.clock,
            verify_files=verify_files,
            emergency_dir=emergency_dir,
        )
        self._n = 0

    def _duration(self, path: Path) -> float:
        seg = self.db.find_by_path(path)
        return seg.duration_s if seg else 0.0

    def add(self, seg_id: str, kind: str = "music", *, duration: float = 200.0,
            tags: list[str] | None = None, exists: bool = True,
            expires_at: datetime | None = None) -> Path:
        path = self.tmp / f"{seg_id}.mp3"
        if exists:
            path.write_bytes(b"x")
        self._n += 1
        self.db.add_segment(Segment(
            id=seg_id, kind=kind, factual=kind != "music", path=path, duration_s=duration,
            created_at=datetime(2026, 1, 1, 0, 0, self._n, tzinfo=MADRID), producer="test",
            expires_at=expires_at, meta={"title": seg_id, "tags": tags or []},
        ))
        return path

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
    assert outcome.duration_s == 180.0 and outcome.reason.startswith("music")
    plays = st.db.list_play_log()
    assert len(plays) == 1
    assert plays[0].started_at == T0
    assert plays[0].ended_at == T0 + timedelta(seconds=180)
    assert plays[0].skipped is False
    assert (plays[0].segment_id, plays[0].kind, plays[0].mode) == ("m1", "music", "default")
    assert st.status("m1") == "ready"


def test_mode_is_logged(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.playout.mode = "tinydesk"
    st.add("m1")
    st.playout.step()
    assert st.db.list_play_log()[0].mode == "tinydesk"


def test_music_excludes_last_artist(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("a1", tags=["artist:a"])
    st.add("a2", tags=["artist:a"])
    st.add("b1", tags=["artist:b"])

    first = st.playout.step()
    second = st.playout.step()

    assert first is not None and second is not None
    assert first.segment_id == "a1"
    # a2 nunca se ha emitido y es más antiguo, pero es del mismo artista
    assert second.segment_id == "b1"


def test_music_falls_back_when_only_same_artist(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("a1", tags=["artist:a"])
    st.add("a2", tags=["artist:a"])
    ids = [o.segment_id for o in (st.playout.step(), st.playout.step()) if o]
    assert ids == ["a1", "a2"]


def test_music_avoids_crossing_time_signal_window(tmp_path: Path) -> None:
    now = datetime(2026, 1, 5, 10, 50, tzinfo=MADRID)
    st = Station(tmp_path, now)
    st.add("long", duration=900.0)   # acabaría a las 11:05 → pisaría la señal
    st.add("short", duration=200.0)
    st.add("ts11", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 11, tzinfo=MADRID))])

    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "short"


def test_music_deadline_ignored_without_next_signal(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 50, tzinfo=MADRID))
    st.add("long", duration=900.0)
    st.add("short", duration=200.0)
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "long"


# ── Señal horaria ─────────────────────────────────────────────────────────────

def test_time_signal_matches_current_local_hour(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 2, tzinfo=MADRID))
    st.add("m1")
    st.add("ts11", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 11, tzinfo=MADRID))])
    st.add("ts10", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 10, tzinfo=MADRID))])

    outcome = st.playout.step()

    assert outcome is not None and outcome.segment_id == "ts10"
    assert st.status("ts10") == "retired"
    assert st.status("ts11") == "ready"


def test_expired_time_signal_is_not_aired(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 2, tzinfo=MADRID))
    st.add("m1")
    st.add("ts10", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 10, tzinfo=MADRID))],
           expires_at=datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "m1"
    assert st.status("ts10") == "ready"   # caducarla es cosa del productor


def test_time_signal_matches_local_hour_from_utc_clock(tmp_path: Path) -> None:
    # 09:02 UTC = 10:02 en Madrid (invierno)
    st = Station(tmp_path, datetime(2026, 1, 5, 9, 2, tzinfo=ZoneInfo("UTC")))
    st.add("m1")
    st.add("ts10", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 10, tzinfo=MADRID))])
    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "ts10"


def test_time_signal_without_match_falls_back_to_scheduler(tmp_path: Path) -> None:
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 2, tzinfo=MADRID))
    st.add("m1")
    st.add("ts11", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 11, tzinfo=MADRID))])

    outcome = st.playout.step()

    assert outcome is not None and outcome.segment_id == "m1"
    assert st.status("ts11") == "ready"


def test_history_across_timezones(tmp_path: Path) -> None:
    # Una señal emitida (registrada con reloj UTC) activa el cooldown en hora local
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 2, tzinfo=MADRID))
    st.add("m1")
    st.add("old", "time_signal", duration=3.0)
    pid = st.db.log_play_start("old", "time_signal", "default",
                               datetime(2026, 1, 5, 9, 0, 5, tzinfo=ZoneInfo("UTC")))
    st.db.log_play_end(pid, datetime(2026, 1, 5, 9, 0, 8, tzinfo=ZoneInfo("UTC")))
    st.add("ts10", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 10, tzinfo=MADRID))])

    outcome = st.playout.step()
    assert outcome is not None and outcome.segment_id == "m1"


# ── Estados y errores ─────────────────────────────────────────────────────────

def test_missing_file_quarantines_and_retries(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("gone", exists=False)
    st.add("ok")

    outcome = st.playout.step()

    assert outcome is not None and outcome.segment_id == "ok"
    assert st.status("gone") == "quarantined"
    assert [p.segment_id for p in st.db.list_play_log()] == ["ok"]


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


def test_talk_retired_after_airing(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("m1")
    st.add("intro", "host_intro", duration=15.0)
    outcome = st.playout.step()   # historial vacío → la palabra está permitida
    assert outcome is not None and outcome.segment_id == "intro"
    assert st.status("intro") == "retired"


def test_jingle_stays_ready(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("j1", "jingle", duration=8.0)
    outcome = st.playout.step()
    assert outcome is not None and outcome.kind == "jingle"
    assert st.status("j1") == "ready"


def test_interrupted_talk_stays_ready(tmp_path: Path) -> None:
    st = Station(tmp_path)
    st.add("intro", "host_intro", duration=15.0)

    class InterruptingAudio(NullAudioBackend):
        def play(self, path: Path) -> None:
            super().play(path)
            st.playout.interrupt()

    st.playout.audio = InterruptingAudio()
    outcome = st.playout.step()

    assert outcome is not None
    assert st.status("intro") == "ready"
    assert st.db.list_play_log()[0].skipped is True


# ── Emergencia ────────────────────────────────────────────────────────────────

def test_nothing_to_play_without_emergency(tmp_path: Path) -> None:
    st = Station(tmp_path, sim_audio=False)
    assert st.playout.step() is None
    assert st.null_audio.calls == []
    assert st.playout.last_emergency is None


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


@pytest.mark.parametrize("window", [0, 120])
def test_history_window_limits_records(tmp_path: Path, window: int) -> None:
    # Con ventana 0 la señal emitida hace 1 min no cuenta y se repite la selección
    st = Station(tmp_path, datetime(2026, 1, 5, 10, 2, tzinfo=MADRID))
    st.playout.history_window = timedelta(minutes=window)
    st.add("m1")
    st.add("old", "time_signal", duration=3.0)
    st.db.log_play_start("old", "time_signal", "default",
                         datetime(2026, 1, 5, 10, 1, tzinfo=MADRID))
    st.add("ts10", "time_signal", duration=3.0,
           tags=[hour_tag(datetime(2026, 1, 5, 10, tzinfo=MADRID))])
    outcome = st.playout.step()
    assert outcome is not None
    assert outcome.segment_id == ("ts10" if window == 0 else "m1")
