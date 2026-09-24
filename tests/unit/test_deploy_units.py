"""
Comprobaciones de la unidad systemd de la emisora (ARCHITECTURE.md §10, §1 inv. 2).
"""

from __future__ import annotations

import configparser
from pathlib import Path

UNIT = Path(__file__).resolve().parents[2] / "deploy" / "radio-station.service"


def load_unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(UNIT)
    return parser


def test_station_unit_restarts_always_and_runs_station() -> None:
    unit = load_unit()
    service = unit["Service"]
    assert service["Type"] == "simple"
    assert service["Restart"] == "always"
    assert service["RestartSec"] == "2"
    assert service["User"] == "radio"
    assert service["ExecStart"].endswith("radio station")
    assert service["EnvironmentFile"].startswith("-")
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_station_unit_does_not_require_network() -> None:
    section = load_unit()["Unit"]
    assert "network-online.target" in section["After"]
    assert "sound.target" in section["After"]
    for key in ("Requires", "Wants", "BindsTo", "Requisite"):
        assert "network" not in section.get(key, "")
