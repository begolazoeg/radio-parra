"""
Tests de los comandos `radio stock` y `radio doctor`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from radio.cli import app
from radio.core.models import Segment
from radio.core.store import DB

REPO = Path(__file__).parents[2]


def seg(seg_id: str, kind: str, **kw: object) -> Segment:
    data: dict[str, object] = {
        "id": seg_id, "kind": kind, "factual": kind != "music",
        "path": Path(f"/x/{seg_id}.wav"), "duration_s": 3.0,
        "created_at": datetime.now(UTC), "producer": "test",
    }
    data.update(kw)
    return Segment(**data)  # type: ignore[arg-type]


def test_stock_reports_targets_statuses_and_expirations(tmp_path: Path) -> None:
    db_file = tmp_path / "state.db"
    soon = datetime.now(UTC) + timedelta(minutes=30)
    with DB(db_file) as db:
        db.add_segment(seg("t1", "time_signal", expires_at=soon,
                           meta={"title": "Señal horaria 11:00"}))
        db.add_segment(seg("t0", "time_signal", status="expired",
                           expires_at=datetime.now(UTC) - timedelta(hours=1)))
        db.add_segment(seg("m1", "music"))
        db.add_segment(seg("m2", "music", status="quarantined"))

    result = CliRunner().invoke(
        app, ["stock", "--config-dir", str(REPO / "config"), "--db", str(db_file)]
    )
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    # time_signal: 1 ready frente a objetivo 2 (activo en producers.yaml)
    assert lines["time_signal"].split()[1:3] == ["1", "2"]
    assert "expired: 1" in lines["time_signal"] and "ready: 1" in lines["time_signal"]
    # music: music_tinydesk activo en producers.yaml → objetivo 30
    assert lines["music"].split()[1:3] == ["1", "30"]
    assert "quarantined: 1" in lines["music"]
    assert "Próximas caducidades" in result.output
    assert "Señal horaria 11:00" in result.output


def test_stock_without_db(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["stock", "--config-dir", str(REPO / "config"), "--db", str(tmp_path / "x.db")]
    )
    assert result.exit_code == 0
    assert "No existe la BD" in result.output


def test_doctor_reports_legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(tmp_path / "data" / "state.db")
    conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(REPO / "config")])
    assert result.exit_code == 0, result.output
    assert "data/state.db" in result.output and "bórrala" in result.output


def test_doctor_reports_schema_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    DB(tmp_path / "data" / "state.db").close()
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(REPO / "config")])
    assert "esquema v1" in result.output


def test_doctor_checks_phase1_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(REPO / "config")])
    assert result.exit_code == 0, result.output
    out = result.output
    for label in ("mpv", "ffmpeg", "data/ escribible", "data/ espacio libre",
                  "bucle de emergencia", "emergency_loop.wav", "productores activos",
                  "https://feeds.npr.org/510306/podcast.xml", ".env"):
        assert label in out, label
    assert "feed accesible" not in out            # sin --network no hay red


def test_doctor_flags_missing_feed_url_and_emergency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "producers.yaml").write_text(
        "producers:\n  music_tinydesk: {active: true, params: {}}\n", encoding="utf-8"
    )
    (config / "station.yaml").write_text(
        "playout: {emergency_dir: nada}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(config)])
    lines = {line.split("] ", 1)[1].split(" — ")[0]: line for line in result.output.splitlines()
             if "] " in line}
    assert "ERROR" in lines["music_tinydesk feed_url"]
    assert "ERROR" in lines["bucle de emergencia (nada)"]


def test_doctor_network_heads_the_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    calls: list[str] = []

    def fake_head(url: str, **_kw: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(httpx, "head", fake_head)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["doctor", "--network", "--config-dir", str(REPO / "config")])
    assert calls == ["https://feeds.npr.org/510306/podcast.xml"]
    assert "feed accesible — HTTP 200" in result.output
