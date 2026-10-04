"""
Normalización de volumen en reproducción: cálculo de la ganancia
(``radio.station.gain``), paso de la ganancia de la emisora al backend
(``enqueue(path, gain_db=...)``) y ``loadfile`` de mpv con opciones por archivo en las
dos formas (mpv < 0.38 y ≥ 0.38) contra ``tests/fixtures/fake_mpv.py``.

El archivo nunca se modifica: la ganancia es del reproductor y solo para ese archivo.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from radio.core.clock import FakeClock
from radio.core.config import AudioConfig, RadioConfig
from radio.core.models import Segment
from radio.providers.audio import (
    EventRecorder,
    FakeEventBackend,
    MpvIpcBackend,
    NullAudioBackend,
    QueueingAudioBackend,
)
from radio.providers.audio.mpv import gain_filter, loadfile_style, parse_mpv_version
from radio.sim import SIM_START, run_simulation
from radio.station import GainPolicy, StationEngine, playback_gain_db, segment_gain_db
from tests.unit.test_station_engine import MADRID, T0, Rig, at, grid, make_wav

REPO = Path(__file__).parents[2]
FAKE_MPV = REPO / "tests" / "fixtures" / "fake_mpv.py"
POLICY = GainPolicy(target_lufs=-16.0, min_db=-12.0, max_db=6.0, peak_ceiling_db=-1.0)


# ── Cálculo ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("lufs", "peak", "expected"),
    [
        (-21.0, -8.0, 5.0),        # más bajo que el objetivo: se sube
        (-12.0, -0.5, -4.0),       # más alto: se baja
        (-16.0, -3.0, 0.0),        # ya en el objetivo
        (-30.0, -20.0, 6.0),       # tope de subida (+6)
        (-1.0, 0.0, -12.0),        # tope de bajada (−12)
        (-20.0, -2.0, 1.0),        # +4 limitado por el pico: −2 + g ≤ −1 → g = +1
        (-18.0, -0.2, -0.8),       # el pico ya pasa del techo: se baja un poco
        (-18.0, None, 2.0),        # sin pico medido: solo el objetivo
        (-16.02, -5.0, 0.0),       # diferencia inapreciable → 0 dB
    ],
)
def test_playback_gain(lufs: float, peak: float | None, expected: float) -> None:
    assert playback_gain_db(lufs, peak, POLICY) == pytest.approx(expected)


def test_peak_limit_never_below_min_cut() -> None:
    assert playback_gain_db(-16.0, 20.0, POLICY) == -12.0


@pytest.mark.parametrize("lufs", [None, math.nan, math.inf, -math.inf, "−18", True])
def test_missing_or_invalid_measurement_is_zero(lufs: Any) -> None:
    assert playback_gain_db(lufs, -1.0, POLICY) == 0.0


def test_disabled_policy_is_zero() -> None:
    assert playback_gain_db(-30.0, -20.0, GainPolicy(enabled=False)) == 0.0


def test_segment_gain_reads_meta() -> None:
    def seg(**meta: Any) -> Segment:
        return Segment(id="s", kind="music", factual=False, path=Path("/x.mp3"),
                       duration_s=1.0, created_at=T0, producer="test", meta=meta)

    assert segment_gain_db(seg(loudness_lufs=-20.0, true_peak_db=-6.0), POLICY) == 4.0
    assert segment_gain_db(seg(loudness_lufs=None, true_peak_db=None), POLICY) == 0.0
    assert segment_gain_db(seg(), POLICY) == 0.0                 # palabra ya normalizada
    assert segment_gain_db(None, POLICY) == 0.0                  # bucle de emergencia


def test_policy_from_config() -> None:
    config = RadioConfig.load(REPO / "config")
    policy = GainPolicy.from_config(config)
    assert policy == GainPolicy(enabled=True, target_lufs=config.station.loudness_lufs,
                                min_db=-12.0, max_db=6.0, peak_ceiling_db=-1.0)
    off = config.model_copy(deep=True)
    off.station.audio.normalize = False
    assert GainPolicy.from_config(off).enabled is False


def test_audio_config_validation() -> None:
    assert AudioConfig().normalize is True
    with pytest.raises(ValidationError):
        AudioConfig(normalize="yes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AudioConfig(gain_max_db=-1.0)
    with pytest.raises(ValidationError):
        AudioConfig(gain_min_db=3.0)


# ── Backends falsos ───────────────────────────────────────────────────────────

def test_fake_backends_record_gain(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    audio = FakeEventBackend(clock)
    assert isinstance(audio, QueueingAudioBackend)
    a, b, c = Path("a.wav"), Path("b.wav"), Path("c.wav")
    audio.enqueue(a, gain_db=3.5)
    audio.enqueue(b)
    audio.enqueue(c, gain_db=-2.0)
    assert audio.current_gain_db() == 3.5
    audio.finish()
    assert audio.current() == b and audio.current_gain_db() == 0.0   # no hereda la de a
    audio.clear_pending()
    audio.finish()
    assert audio.current_gain_db() is None
    assert audio.gains == [(a, 3.5), (b, 0.0)]
    assert [c.get("gain_db") for c in audio.calls if c["action"] == "enqueue"] == [3.5, 0.0, -2.0]

    null = NullAudioBackend()
    null.enqueue(a, gain_db=1.25)
    null.enqueue(b)
    assert [c["gain_db"] for c in null.calls] == [1.25, 0.0]


# ── Emisora → backend ─────────────────────────────────────────────────────────

def add_measured(rig: Rig, seg_id: str, lufs: float | None, peak: float | None,
                 *, kind: str = "music", duration: float = 200.0) -> Path:
    path = make_wav(rig.tmp / f"{seg_id}.wav")
    rig._n += 1
    meta: dict[str, Any] = {"title": seg_id, "tags": [f"artist:{seg_id}"]}
    if lufs is not None or peak is not None:
        meta.update(loudness_lufs=lufs, true_peak_db=peak)
    rig.db.add_segment(Segment(
        id=seg_id, kind=kind, factual=kind != "music", path=path, duration_s=duration,
        created_at=datetime(2026, 1, 1, 0, 0, rig._n, tzinfo=MADRID), producer="test",
        meta=meta,
    ))
    return path


def enqueued_gains(rig: Rig) -> dict[str, float]:
    return {Path(str(c["path"])).stem: float(c["gain_db"])  # type: ignore[arg-type]
            for c in rig.backend.calls if c["action"] == "enqueue"}


def test_engine_passes_gain_per_segment(tmp_path: Path) -> None:
    rig = Rig(tmp_path, gain_policy=POLICY)
    add_measured(rig, "quiet", -22.0, -9.0)
    add_measured(rig, "loud", -11.0, -0.3)
    add_measured(rig, "unmeasured", None, None)
    rig.engine.start()
    assert enqueued_gains(rig) == {"quiet": 6.0, "loud": -5.0, "unmeasured": 0.0}
    # El espejo de la cola guarda la ganancia de cada elemento
    items = [rig.engine.queue.current, *rig.engine.queue.pending]
    assert {i.path.stem: i.gain_db for i in items if i is not None} == enqueued_gains(rig)
    # Se aplica al sonar: el backend la tiene para el archivo en curso
    cur = rig.backend.current()
    assert cur is not None and rig.backend.current_gain_db() == enqueued_gains(rig)[cur.stem]


def test_engine_normalize_off_sends_zero(tmp_path: Path) -> None:
    rig = Rig(tmp_path, gain_policy=GainPolicy(enabled=False))
    add_measured(rig, "quiet", -22.0, -9.0)
    add_measured(rig, "loud", -11.0, -0.3)
    rig.engine.start()
    assert set(enqueued_gains(rig).values()) == {0.0}


def test_interrupt_signal_zero_gain_music_keeps_its_own(tmp_path: Path) -> None:
    """La señal horaria (palabra normalizada en post) va a 0 dB; la música, con la suya."""
    rig = Rig(tmp_path, at(10, 58), grid_config=grid(), gain_policy=POLICY)
    for i in range(4):
        add_measured(rig, f"m{i}", -20.0 - i, -8.0, duration=300.0)
    rig.signal("sig", at(11))
    rig.engine.start()
    rig.run_until(at(11, 10))
    played = {p.stem: g for p, g in rig.backend.gains}
    assert played["sig"] == 0.0
    music = {k: v for k, v in played.items() if k.startswith("m")}
    assert len(music) >= 2
    for stem, gain in music.items():
        n = int(stem[1:])
        assert gain == pytest.approx(min(4.0 + n, 6.0, -1.0 - (-8.0)))
    assert ("sig", "time_signal", False) in rig.log()


def test_engine_gain_stats(tmp_path: Path) -> None:
    rig = Rig(tmp_path, gain_policy=POLICY)
    add_measured(rig, "a", -20.0, -9.0)        # +4
    add_measured(rig, "b", -13.0, -4.0)        # −3
    add_measured(rig, "c", None, None)         # sin medida: no cuenta
    rig.engine.start()
    rig.run_until(T0 + timedelta(seconds=650))
    g = rig.engine.stats.gain
    assert g.count >= 2 and g.min_db == -3.0 and g.max_db == 4.0
    assert g.mean_db is not None and -3.0 <= g.mean_db <= 4.0


def test_from_config_uses_station_audio(tmp_path: Path) -> None:
    config = RadioConfig.load(REPO / "config")
    rig = Rig(tmp_path)
    engine = StationEngine.from_config(config, rig.db, rig.backend, rig.clock)
    assert engine.gain_policy == GainPolicy.from_config(config)


def test_simulation_reports_gain_stats() -> None:
    config = RadioConfig.load(REPO / "config")
    report = run_simulation(hours=2, seed=1, config=config, start=SIM_START)
    g = report.gain_db
    assert isinstance(g["measured"], int) and g["measured"] > 0
    assert isinstance(g["min"], float) and isinstance(g["max"], float)
    assert -12.0 <= g["min"] <= g["max"] <= 6.0
    assert "Ganancia en reproducción" in report.to_text()
    assert report.passed


# ── mpv: versión y loadfile ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "parsed", "style"),
    [
        ("mpv 0.35.1", (0, 35), "legacy"),
        ("mpv 0.37.0-dirty", (0, 37), "legacy"),
        ("mpv 0.38.0", (0, 38), "index"),
        ("mpv v0.39.0-12-gabcdef", (0, 39), "index"),
        ("mpv 1.0.0", (1, 0), "index"),
        ("mpv git-master", None, "unknown"),
        (None, None, "unknown"),
    ],
)
def test_parse_mpv_version(text: str | None, parsed: tuple[int, int] | None, style: str) -> None:
    assert parse_mpv_version(text) == parsed
    assert loadfile_style(text) == style


def test_gain_filter_quotes_value() -> None:
    assert gain_filter(3.5) == "[lavfi-volume=volume=3.50dB]"
    assert gain_filter(-2) == "[lavfi-volume=volume=-2.00dB]"
    assert gain_filter(1.0, "lavfi-highpass=f=40") == (
        "[lavfi-highpass=f=40,lavfi-volume=volume=1.00dB]"
    )


def make_backend(extra_args: list[str] | None = None) -> MpvIpcBackend:
    return MpvIpcBackend(
        [sys.executable, str(FAKE_MPV)], extra_args=extra_args,
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
        backoff_initial=0.05, backoff_max=0.2,
    )


def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_events(rec: EventRecorder, n: int) -> None:
    deadline = time.monotonic() + 5.0
    while len(rec.events) < n:
        assert time.monotonic() < deadline, f"solo {len(rec.events)} eventos de {n}"
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("version", "style", "expected_tail"),
    [
        ("mpv 0.35.1", "legacy", ["af=[lavfi-volume=volume=3.50dB]"]),
        ("mpv 0.38.0", "index", [-1, "af=[lavfi-volume=volume=3.50dB]"]),
    ],
    ids=["mpv-0.35", "mpv-0.38"],
)
def test_loadfile_per_file_gain_old_and_new_mpv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    version: str, style: str, expected_tail: list[Any],
) -> None:
    log = tmp_path / "mpv.jsonl"
    monkeypatch.setenv("FAKE_MPV_LOG", str(log))
    monkeypatch.setenv("FAKE_MPV_VERSION", version)
    a = make_wav(tmp_path / "a.wav", 0.1)
    b = make_wav(tmp_path / "b.wav", 0.1)
    c = make_wav(tmp_path / "c.wav", 0.1)
    rec = EventRecorder()
    with make_backend() as backend:
        backend.add_listener(rec)
        assert backend.mpv_version == version and backend.loadfile_style == style
        backend.enqueue(a, gain_db=3.5)
        backend.enqueue(b)                      # sin ganancia
        backend.enqueue(c, gain_db=-4.25)
        wait_events(rec, 6)
    assert [e.reason for e in rec.events if hasattr(e, "reason")] == ["eof"] * 3
    entries = read_log(log)
    loads = [e["loadfile"] for e in entries if "loadfile" in e]
    assert loads[0] == ["loadfile", str(a), "append-play", *expected_tail]
    assert loads[1] == ["loadfile", str(b), "append-play"]      # 0 dB: sin opciones
    assert loads[2][-1] == "af=[lavfi-volume=volume=-4.25dB]"
    # Aislamiento: la ganancia solo está en vigor mientras suena su archivo
    starts = [(Path(e["start"]).name, e["af"]) for e in entries if "start" in e]
    assert starts == [
        ("a.wav", "lavfi-volume=volume=3.50dB"),
        ("b.wav", ""),
        ("c.wav", "lavfi-volume=volume=-4.25dB"),
    ]
    assert a.read_bytes() == make_wav(tmp_path / "ref.wav", 0.1).read_bytes()  # sin tocar


def test_fake_mpv_rejects_wrong_loadfile_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El mpv falso es estricto como el real: la forma equivocada da error."""
    monkeypatch.setenv("FAKE_MPV_VERSION", "mpv 0.38.0")
    a = make_wav(tmp_path / "a.wav", 0.1)
    with make_backend() as backend:
        backend.start()
        with pytest.raises(Exception, match="invalid parameter"):
            backend._request(["loadfile", str(a), "append-play", "af=[lavfi-volume=volume=1dB]"])
    monkeypatch.setenv("FAKE_MPV_VERSION", "mpv 0.35.1")
    with make_backend() as backend:
        backend.start()
        with pytest.raises(Exception, match="invalid parameter"):
            backend._request(["loadfile", str(a), "append-play", -1,
                              "af=[lavfi-volume=volume=1dB]"])
        with pytest.raises(Exception, match="invalid parameter"):   # "=" sin corchetes
            backend._request(["loadfile", str(a), "append-play", "af=lavfi-volume=volume=1dB"])


def test_unknown_mpv_version_plays_without_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = tmp_path / "mpv.jsonl"
    monkeypatch.setenv("FAKE_MPV_LOG", str(log))
    monkeypatch.setenv("FAKE_MPV_VERSION", "none")
    a = make_wav(tmp_path / "a.wav", 0.1)
    rec = EventRecorder()
    with caplog.at_level("WARNING"), make_backend() as backend:
        backend.add_listener(rec)
        assert backend.mpv_version is None and backend.loadfile_style == "unknown"
        backend.enqueue(a, gain_db=5.0)
        wait_events(rec, 2)
    assert rec.events[-1].reason == "eof"  # type: ignore[union-attr]
    [load] = [e["loadfile"] for e in read_log(log) if "loadfile" in e]
    assert load == ["loadfile", str(a), "append-play"]
    assert "sin ganancia" in caplog.text


def test_global_af_is_kept_during_gain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "mpv.jsonl"
    monkeypatch.setenv("FAKE_MPV_LOG", str(log))
    a = make_wav(tmp_path / "a.wav", 0.1)
    b = make_wav(tmp_path / "b.wav", 0.1)
    rec = EventRecorder()
    with make_backend(["--af=lavfi-highpass"]) as backend:
        backend.add_listener(rec)
        assert backend.loadfile_command(a, 2.0) == [
            "loadfile", str(a), "append-play", "af=[lavfi-highpass,lavfi-volume=volume=2.00dB]",
        ]
        backend.enqueue(a, gain_db=2.0)
        backend.enqueue(b)
        wait_events(rec, 4)
    starts = [(Path(e["start"]).name, e["af"]) for e in read_log(log) if "start" in e]
    assert starts == [("a.wav", "lavfi-highpass,lavfi-volume=volume=2.00dB"),
                      ("b.wav", "lavfi-highpass")]


def test_watchdog_relaunch_keeps_gain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "mpv.jsonl"
    monkeypatch.setenv("FAKE_MPV_LOG", str(log))
    monkeypatch.setenv("FAKE_MPV_VERSION", "mpv 0.38.1")
    a = make_wav(tmp_path / "a.wav", 0.1)
    crash = make_wav(tmp_path / "crash.wav", 0.1)
    b = make_wav(tmp_path / "b.wav", 0.1)
    rec = EventRecorder()
    with make_backend() as backend:
        backend.add_listener(rec)
        backend.enqueue(a)
        backend.enqueue(crash)
        backend.enqueue(b, gain_db=-3.0)
        wait_events(rec, 6)
        assert backend.restarts == 1 and backend.loadfile_style == "index"
    starts = [(Path(e["start"]).name, e["af"]) for e in read_log(log) if "start" in e]
    assert starts[-1] == ("b.wav", "lavfi-volume=volume=-3.00dB")
    b_loads = [e["loadfile"] for e in read_log(log)
               if "loadfile" in e and e["loadfile"][1] == str(b)]
    assert len(b_loads) == 2                                   # antes y después del relanzamiento
    assert all(load[3:] == [-1, "af=[lavfi-volume=volume=-3.00dB]"] for load in b_loads)
