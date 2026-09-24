"""
Tests de la simulación acelerada (radio simulate).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from radio.cli import app
from radio.core.config import RadioConfig
from radio.sim import SIM_START, SimReport, run_simulation

REPO = Path(__file__).parents[2]


def simulate(hours: float = 24.0, seed: int = 1, start: datetime = SIM_START) -> SimReport:
    return run_simulation(
        hours=hours,
        seed=seed,
        config=RadioConfig.load(REPO / "config"),
        prompts_dir=REPO / "prompts",
        start=start,
    )


@pytest.fixture(scope="module")
def report_24h() -> SimReport:
    return simulate()


def test_24h_seed1_passes_invariants(report_24h: SimReport) -> None:
    r = report_24h
    assert r.passed, r.failures
    assert r.dead_air_s == 0
    assert r.back_to_back_artist == 0
    assert r.producer_errors == 0
    assert r.time_signals_aired >= 23
    assert r.time_signals_on_time == r.time_signals_aired
    assert r.max_talk_ratio_rolling_hour <= r.talk_budget_ratio
    assert r.music_share > 0.95          # solo música, jingles y señal horaria
    assert r.airtime_s["jingle"] > 0 and r.airtime_s["time_signal"] > 0
    assert r.airtime_s["host_intro"] == 0      # host_intro vuelve en Fase 2
    assert r.airtime_s["factual"] == 0 and r.airtime_s["fiction"] == 0
    assert r.producer_runs >= 47               # cron */30 durante 24 h
    assert sum(r.airtime_s.values()) >= 24 * 3600
    assert len(r.decisions_sample) > 0


def test_same_seed_is_deterministic(report_24h: SimReport) -> None:
    assert simulate().to_dict() == report_24h.to_dict()


def test_different_seeds_differ(report_24h: SimReport) -> None:
    assert simulate(seed=2).to_dict() != report_24h.to_dict()


def test_custom_start_and_text_report() -> None:
    start = datetime(2026, 7, 1, 13, 30, tzinfo=ZoneInfo("Europe/Madrid"))
    r = simulate(hours=3, seed=5, start=start)
    assert r.passed, r.failures
    assert r.start == start.isoformat()
    text = r.to_text()
    assert "RESULTADO: OK" in text and "Señales horarias" in text


def test_invariant_failure_is_reported() -> None:
    r = simulate(hours=2)
    r.dead_air_s = 5.0
    r.failures.append("silencio en antena: 5 s")
    assert not r.passed
    assert "RESULTADO: FALLO" in r.to_text()


def test_cli_simulate_json() -> None:
    result = CliRunner().invoke(
        app,
        ["simulate", "--hours", "2", "--seed", "3", "--json",
         "--config-dir", str(REPO / "config"), "--prompts-dir", str(REPO / "prompts")],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["passed"] is True and data["seed"] == 3
