"""
Criterios de aceptación de la Fase 1 (ARCHITECTURE.md §12), como tests:

- "cortando la red sigue sonando": la emisora emite horas sin abrir ni un socket de
  red (solo el socket unix de mpv) y no importa código de red ni de producción (inv. 2);
- "simulate produce una línea de tiempo de 24 h";
- "suena música en bucle" con solo stock musical (modo tinydesk).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import wave
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from radio.cli import app
from radio.core.clock import SystemClock
from radio.core.config import RadioConfig
from radio.core.models import Segment
from radio.core.store import DB
from radio.providers.audio.mpv import MpvIpcBackend
from radio.sim import SIM_START, run_simulation
from radio.station import StationEngine

REPO = Path(__file__).parents[2]
FAKE_MPV = REPO / "tests" / "fixtures" / "fake_mpv.py"


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Cualquier conexión AF_INET/AF_INET6 o petición httpx falla (y queda anotada)."""
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded(sock: socket.socket, address: Any) -> Any:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(f"connect {address!r}")
            raise NetworkBlocked(f"red cortada: connect {address!r}")
        return real_connect(sock, address)

    def guarded_ex(sock: socket.socket, address: Any) -> Any:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(f"connect_ex {address!r}")
            raise NetworkBlocked(f"red cortada: connect_ex {address!r}")
        return real_connect_ex(sock, address)

    def create_connection(address: Any, *_a: Any, **_k: Any) -> Any:
        attempts.append(f"create_connection {address!r}")
        raise NetworkBlocked(f"red cortada: create_connection {address!r}")

    def send(self: Any, request: httpx.Request, *_a: Any, **_k: Any) -> Any:
        attempts.append(f"httpx {request.url}")
        raise NetworkBlocked(f"red cortada: {request.url}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(httpx.Client, "send", send)
    yield attempts


def test_network_block_is_effective(no_network: list[str]) -> None:
    with pytest.raises(NetworkBlocked):
        httpx.get("https://feeds.npr.org/510306/podcast.xml")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(NetworkBlocked):
        s.connect(("93.184.216.34", 80))
    s.close()
    assert len(no_network) == 2
    no_network.clear()


# ── (a) Cortando la red sigue sonando ─────────────────────────────────────────

def test_station_keeps_playing_without_network(no_network: list[str]) -> None:
    """12 h con el motor real (y producción simulada con fakes) sin red: sin huecos."""
    report = run_simulation(
        hours=12, seed=4, config=RadioConfig.load(REPO / "config"),
        prompts_dir=REPO / "prompts", catalog="tinydesk",
    )
    assert report.passed, report.failures
    assert report.dead_air_s == 0 and report.rung_histogram["5"] == 0
    assert report.time_signals_aired >= 11
    assert sum(report.airtime_s.values()) >= 12 * 3600 - 1
    assert no_network == []                    # nadie intentó salir a la red


def make_wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(seconds * 8000))
    return path


def test_station_with_mpv_ipc_plays_without_network(
    tmp_path: Path, no_network: list[str]
) -> None:
    """La emisora real (mpv por socket unix, reloj del sistema) sin red: suena y registra."""
    config = RadioConfig.load(REPO / "config")
    clock = SystemClock(config.station.timezone)
    db = DB(tmp_path / "state.db")
    for i in range(3):
        path = make_wav(tmp_path / f"m{i}.wav", 0.1)
        db.add_segment(Segment(
            id=f"m{i}", kind="music", factual=False, path=path, duration_s=0.1,
            created_at=clock.now() - timedelta(days=1), producer="test",
            meta={"title": f"m{i}", "tags": [f"artist:{i}"]},
        ))
    backend = MpvIpcBackend([sys.executable, str(FAKE_MPV)], clock=clock,
                            backoff_initial=0.05, backoff_max=0.2)
    engine = StationEngine.from_config(config, db, backend, clock, mode="tinydesk",
                                       auto_drain=False)
    try:
        engine.start()
        deadline = time.monotonic() + 10
        while sum(1 for p in db.list_play_log() if p.ended_at) < 6:
            assert time.monotonic() < deadline, "la emisora no avanza"
            engine.tick()
            time.sleep(0.01)
    finally:
        engine.stop()
        backend.close()
        engine.drain()
    plays = db.list_play_log()
    assert {p.kind for p in plays} == {"music"}
    assert all(p.ended_at is not None for p in plays)          # todo cerrado al parar
    ids = [p.segment_id for p in plays]
    assert all(a != b for a, b in zip(ids, ids[1:], strict=False))
    assert no_network == []
    db.close()


def test_station_imports_no_network_or_production_code() -> None:
    """Invariante 2: la emisora no carga productores, LLM, TTS ni clientes de red."""
    code = (
        "import sys, radio.station, radio.station.service\n"
        "bad = [m for m in ('httpx', 'feedparser', 'radio.producers', 'radio.providers.llm',"
        " 'radio.providers.tts', 'radio.providers.registry', 'radio.music.feed')"
        " if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=REPO)
    assert out.stdout.strip() == ""


# ── (b) simulate produce una línea de tiempo de 24 h ──────────────────────────

def test_simulate_prints_24h_timeline() -> None:
    result = CliRunner().invoke(app, [
        "simulate", "--hours", "24", "--seed", "1", "--timeline",
        "--config-dir", str(REPO / "config"), "--prompts-dir", str(REPO / "prompts"),
    ])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    rows = [line for line in lines if line[:2].isdigit() and line[2] == " "]
    assert rows[0].startswith("05 00:00:00")
    assert rows[-1].startswith("05 23:")
    assert any("time_signal" in r and "12:00" in r for r in rows)
    assert "RESULTADO: OK" in result.output


def test_timeline_is_continuous_for_24h() -> None:
    report = run_simulation(hours=24, seed=1, config=RadioConfig.load(REPO / "config"),
                            prompts_dir=REPO / "prompts")
    assert report.passed, report.failures
    items = report.timeline
    assert items[0].at == "05 00:00:00"
    total = sum(t.duration_s for t in items)
    assert abs(total - 24 * 3600) < 1.0        # sin huecos ni solapes
    assert report.to_dict(timeline=True)["timeline"][0]["kind"] == "music"
    assert "timeline" not in report.to_dict()


# ── (c) Música en bucle con solo stock musical ────────────────────────────────

def test_tinydesk_mode_loops_music_only_for_48h() -> None:
    report = run_simulation(hours=48, seed=5, config=RadioConfig.load(REPO / "config"),
                            prompts_dir=REPO / "prompts", mode="tinydesk", catalog="tinydesk",
                            start=SIM_START)
    assert report.passed, report.failures
    # Modo tinydesk = solo música + las intros del locutor pegadas a cada concierto
    # (Fase 2); ningún otro kind suena
    on_air = report.airtime_s["music"] + report.airtime_s["host_intro"]
    assert on_air >= 48 * 3600 - 1
    assert report.airtime_s["music"] / on_air > 0.99
    assert {k for k, v in report.airtime_s.items() if v > 0} <= {"music", "host_intro"}
    assert report.units_aired > 40            # 40 conciertos: se repiten (bucle)
    assert report.dead_air_s == 0 and report.rung_histogram["5"] == 0
