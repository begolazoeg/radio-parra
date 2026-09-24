"""
Tests de radio.grid.rules y de la validación de grid.yaml: expresiones ``when``,
franjas (medianoche, "24:00", DST), modos e instantes de interrupción.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from radio.core.config import GridConfig, InterruptRule, RadioConfig
from radio.grid.rules import (
    FALLBACK_DAYPART,
    daypart_for,
    hour_tag,
    last_fire_at,
    next_interrupt_at,
    parse_when,
    resolve_mode,
)
from radio.producers.time_signal import hour_tag as producer_hour_tag
from tests.fixtures.grid import DOC_GRID, MADRID, doc_grid, local, simple_grid

REPO = Path(__file__).parents[2]


# ── when ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("expr", "minutes"), [
    ("minute == 0", {0}),
    ("minute==30", {30}),
    ("  minute  ==  59 ", {59}),
    ("minute in [0, 30]", {0, 30}),
    ("minute in [15,45,]", {15, 45}),
    ("minute in [7]", {7}),
])
def test_parse_when_accepts_grammar(expr: str, minutes: set[int]) -> None:
    assert parse_when(expr).minutes == frozenset(minutes)


@pytest.mark.parametrize("expr", [
    "", "minute", "minute = 0", "minute == 60", "minute == -1", "hour == 0",
    "minute in []", "minute in [0, 99]", "minute == 0 or 1", "__import__('os')",
    "minute == 0; import os", "minute in (0, 30)", "minute == 0.5", "MINUTE == 0",
])
def test_parse_when_rejects_junk(expr: str) -> None:
    with pytest.raises(ValueError):
        parse_when(expr)


def test_invalid_when_fails_config_validation() -> None:
    with pytest.raises(ValidationError):
        InterruptRule(kind="time_signal", when="eval('1')")


# ── Configuración ─────────────────────────────────────────────────────────────

def test_repo_grid_yaml_has_doc_shape() -> None:
    grid = RadioConfig.load(REPO / "config").grid
    assert grid.timezone == "Europe/Madrid"
    assert (grid.talk_budget.window_minutes, grid.talk_budget.max_ratio) == (60, 0.22)
    assert grid.cooldowns_minutes["consultorio"] == 90
    assert set(grid.modes) == {"default", "tinydesk"}
    default = grid.modes["default"]
    assert [r.kind for r in default.interrupts] == ["time_signal"]
    assert default.interrupts[0].max_late_seconds == 90
    assert [p.name for p in default.dayparts] == ["manana", "tarde", "noche"]
    assert default.dayparts[0].talk_pool == {
        "weather": 3, "ephemeris": 2, "horoscope": 2, "word_of_day": 1,
    }
    tinydesk = grid.modes["tinydesk"]
    assert tinydesk.interrupts == [] and tinydesk.dayparts[0].pattern == ["music"]


def test_doc_example_validates() -> None:
    grid = doc_grid()
    assert grid.modes["default"].dayparts[2].from_ == "20:00"


@pytest.mark.parametrize("patch", [
    {"talk_budget": {"window_minutes": 0, "max_ratio": 0.2}},
    {"talk_budget": {"window_minutes": 60, "max_ratio": 1.5}},
    {"cooldowns_minutes": {"weather": -1}},
    {"modes": {}},
    {"unknown_key": 1},
])
def test_invalid_grid_values(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GridConfig.model_validate({**DOC_GRID, **patch})


@pytest.mark.parametrize("part", [
    {"name": "x", "from": "25:00", "to": "07:00", "pattern": ["music"]},
    {"name": "x", "from": "24:00", "to": "07:00", "pattern": ["music"]},
    {"name": "x", "from": "7:00", "to": "08:00", "pattern": ["music"]},
    {"name": "x", "from": "07:00", "to": "08:00", "pattern": []},
    {"name": "x", "from": "07:00", "to": "08:00", "pattern": ["news"]},
    {"name": "x", "from": "07:00", "to": "08:00", "pattern": ["talk"], "talk_pool": {"a": -1}},
])
def test_invalid_dayparts(part: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GridConfig.model_validate({"modes": {"default": {"dayparts": [part]}}})


def test_default_grid_without_yaml_is_music_only() -> None:
    grid = GridConfig()
    assert daypart_for(grid, local(2026, 1, 5, 3), "default").pattern == ["music"]


# ── Franjas ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("h", "m", "name"), [
    (7, 0, "manana"), (11, 59, "manana"), (12, 0, "tarde"), (19, 59, "tarde"),
    (20, 0, "noche"), (23, 59, "noche"), (0, 0, "noche"), (6, 59, "noche"),
])
def test_daypart_boundaries_and_midnight_crossing(h: int, m: int, name: str) -> None:
    assert daypart_for(doc_grid(), local(2026, 1, 5, h, m), "default").name == name


def test_daypart_uses_local_wall_time_from_utc() -> None:
    # 06:30 UTC = 07:30 Madrid en invierno, 08:30 en verano
    grid = doc_grid()
    assert daypart_for(grid, datetime(2026, 1, 5, 5, 59, tzinfo=UTC), "default").name == "noche"
    assert daypart_for(grid, datetime(2026, 1, 5, 6, 0, tzinfo=UTC), "default").name == "manana"
    assert daypart_for(grid, datetime(2026, 7, 5, 5, 0, tzinfo=UTC), "default").name == "manana"


def test_daypart_on_dst_days() -> None:
    grid = doc_grid()
    # 29-03-2026: a las 02:00 se salta a las 03:00 (CET → CEST)
    spring = datetime(2026, 3, 29, 5, 0, tzinfo=UTC)          # 07:00 CEST
    assert daypart_for(grid, spring, "default").name == "manana"
    assert daypart_for(grid, datetime(2026, 3, 29, 4, 59, tzinfo=UTC), "default").name == "noche"
    # 25-10-2026: de 03:00 CEST se vuelve a 02:00 CET
    autumn = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)         # 07:00 CET
    assert daypart_for(grid, autumn, "default").name == "manana"
    assert daypart_for(grid, datetime(2026, 10, 25, 5, 59, tzinfo=UTC), "default").name == "noche"


def test_daypart_00_to_24_covers_whole_day() -> None:
    grid = doc_grid()
    for h in range(24):
        assert daypart_for(grid, local(2026, 1, 5, h, 30), "tinydesk").name == "todo"
    assert daypart_for(grid, local(2026, 1, 5, 23, 59, 59), "tinydesk").name == "todo"


def test_daypart_gap_falls_back_to_music() -> None:
    grid = GridConfig.model_validate({"modes": {"default": {"dayparts": [
        {"name": "manana", "from": "07:00", "to": "12:00", "pattern": ["talk"]},
    ]}}})
    assert daypart_for(grid, local(2026, 1, 5, 13), "default") == FALLBACK_DAYPART


def test_daypart_ending_at_24() -> None:
    grid = GridConfig.model_validate({"modes": {"default": {"dayparts": [
        {"name": "tarde", "from": "18:00", "to": "24:00", "pattern": ["music"]},
        {"name": "resto", "from": "00:00", "to": "18:00", "pattern": ["talk"]},
    ]}}})
    assert daypart_for(grid, local(2026, 1, 5, 23, 59), "default").name == "tarde"
    assert daypart_for(grid, local(2026, 1, 6, 0, 0), "default").name == "resto"


def test_unknown_mode_falls_back_to_default() -> None:
    grid = doc_grid()
    assert resolve_mode(grid, "nope")[0] == "default"
    only = GridConfig.model_validate({"modes": {"x": {}}})
    assert resolve_mode(only, "default")[0] == "x"


# ── Interrupciones ────────────────────────────────────────────────────────────

def test_next_interrupt_at_top_of_hour() -> None:
    grid = doc_grid()
    assert next_interrupt_at(grid, local(2026, 1, 5, 10, 20), "default") == local(2026, 1, 5, 11)
    assert next_interrupt_at(grid, local(2026, 1, 5, 11), "default") == local(2026, 1, 5, 12)
    assert next_interrupt_at(grid, local(2026, 1, 5, 23, 59), "default") == local(2026, 1, 6, 0)
    assert next_interrupt_at(grid, local(2026, 1, 5, 10), "tinydesk") is None


def test_next_interrupt_with_minute_list() -> None:
    grid = simple_grid(["music"])
    grid.modes["default"].interrupts.append(
        InterruptRule(kind="time_signal", when="minute in [0, 30]")
    )
    assert next_interrupt_at(grid, local(2026, 1, 5, 10, 20), "default") == local(2026, 1, 5, 10, 30)
    assert next_interrupt_at(grid, local(2026, 1, 5, 10, 30), "default") == local(2026, 1, 5, 11)


def test_interrupt_instants_across_dst() -> None:
    grid = doc_grid()
    # Primavera: tras 01:59 CET la siguiente hora en punto es 03:00 CEST (01:00 UTC)
    nxt = next_interrupt_at(grid, datetime(2026, 3, 29, 0, 59, tzinfo=UTC), "default")
    assert nxt is not None and nxt.astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert nxt.hour == 3
    # Otoño: las 02:00 ocurren dos veces (00:00 y 01:00 UTC)
    first = next_interrupt_at(grid, datetime(2026, 10, 24, 23, 30, tzinfo=UTC), "default")
    second = next_interrupt_at(grid, datetime(2026, 10, 25, 0, 30, tzinfo=UTC), "default")
    assert first is not None and second is not None
    assert (first.hour, second.hour) == (2, 2)
    assert (second.astimezone(UTC) - first.astimezone(UTC)).total_seconds() == 3600


def test_last_fire_in_half_hour_timezone() -> None:
    rule = InterruptRule(kind="time_signal", when="minute == 0")
    kolkata = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 1, 5, 10, 1, tzinfo=kolkata)
    assert last_fire_at(rule, now, kolkata) == datetime(2026, 1, 5, 10, 0, tzinfo=kolkata)


def test_hour_tag_matches_producer_convention() -> None:
    t = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert hour_tag(t, MADRID) == producer_hour_tag(t.astimezone(MADRID)) == "hour:2026-01-05T10"
