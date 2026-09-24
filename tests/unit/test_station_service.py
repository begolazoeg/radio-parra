"""
Test de extremo a extremo de ``radio station`` (``station.service.run_station``) con el
mpv falso de tests/fixtures: arranca, suena, registra y para limpio con SIGTERM.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from radio.core.models import Segment
from radio.core.paths import db_path
from radio.core.store import DB
from radio.station.service import run_station

REPO = Path(__file__).parents[2]
FAKE_MPV = REPO / "tests" / "fixtures" / "fake_mpv.py"


def make_wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(seconds * 8000))
    return path


def test_run_station_plays_and_stops_on_sigterm(tmp_path: Path) -> None:
    # mpv falso como ejecutable (station.yaml → audio.mpv_bin)
    wrapper = tmp_path / "mpv"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_MPV} \"$@\"\n")
    wrapper.chmod(0o755)
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config)
    station = yaml.safe_load((config / "station.yaml").read_text(encoding="utf-8"))
    station["audio"] = {"mpv_bin": str(wrapper), "mpv_args": ["--volume=80"]}
    station["playout"]["emergency_dir"] = str(REPO / "assets" / "emergency")
    (config / "station.yaml").write_text(yaml.safe_dump(station), encoding="utf-8")

    data = tmp_path / "data"
    data.mkdir()
    with DB(db_path(data)) as db:
        for i in range(3):
            db.add_segment(Segment(
                id=f"m{i}", kind="music", factual=False,
                path=make_wav(tmp_path / f"m{i}.wav", 0.1), duration_s=0.1,
                created_at=datetime.now(UTC) - timedelta(days=1), producer="test",
                meta={"title": f"m{i}", "tags": [f"artist:{i}"]},
            ))

    def stop_when_played() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with DB(db_path(data)) as db:
                if sum(1 for p in db.list_play_log() if p.ended_at) >= 4:
                    break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    before = signal.getsignal(signal.SIGTERM)
    watcher = threading.Thread(target=stop_when_played, daemon=True)
    watcher.start()
    code = run_station(config_dir=config, data_dir=data, mode="tinydesk")
    watcher.join(timeout=5)

    assert code == 0
    with DB(db_path(data)) as db:
        plays = db.list_play_log()
    assert sum(1 for p in plays if p.ended_at and not p.skipped) >= 4
    assert all(p.ended_at is not None for p in plays)        # lo último se cerró al parar
    assert {p.mode for p in plays} == {"tinydesk"}
    assert signal.getsignal(signal.SIGTERM) == before      # manejadores restaurados


def test_run_station_without_mpv_exits_with_error(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config)
    station = yaml.safe_load((config / "station.yaml").read_text(encoding="utf-8"))
    station["audio"] = {"mpv_bin": str(tmp_path / "no-existe")}
    (config / "station.yaml").write_text(yaml.safe_dump(station), encoding="utf-8")
    assert run_station(config_dir=config, data_dir=tmp_path / "data") == 1
