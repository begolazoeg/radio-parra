"""
Tests unitarios del Scheduler de parrilla (lógica pura).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from radio.core.config import Cooldowns, GridConfig, TimeSlot
from radio.core.models import SegmentKind
from radio.core.scheduler import TALK_KINDS, PlayRecord, Scheduler

MADRID = ZoneInfo("Europe/Madrid")

ALL: dict[str, int] = {
    "music": 5, "host_intro": 5, "factual": 5, "fiction": 5, "time_signal": 5, "jingle": 5,
}

DURATIONS: dict[str, float] = {
    "music": 240.0, "host_intro": 90.0, "factual": 90.0, "fiction": 90.0,
    "time_signal": 5.0, "jingle": 8.0,
}


def madrid_grid(**overrides: object) -> GridConfig:
    """Parrilla equivalente a config/grid.yaml."""
    data: dict[str, object] = {
        "timezone": "Europe/Madrid",
        "talk_budget_ratio": 0.30,
        "cooldowns_minutes": Cooldowns(factual=15, fiction=30, time_signal=55, jingle=10),
        "time_signal_enabled": True,
        "slots": [
            TimeSlot(name="manana", start="07:00", end="13:00", music_ratio=0.65, max_talk_run_min=4),
            TimeSlot(name="tarde", start="13:00", end="21:00", music_ratio=0.70, max_talk_run_min=3),
            TimeSlot(name="noche", start="21:00", end="07:00", music_ratio=0.80, max_talk_run_min=2),
        ],
    }
    data.update(overrides)
    return GridConfig.model_validate(data)


def local(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=MADRID)


def rec(kind: SegmentKind, start: datetime, duration: float | None = None) -> PlayRecord:
    return PlayRecord(kind=kind, started_at=start, duration_s=duration or DURATIONS[kind])


def only(*kinds: str) -> dict[str, int]:
    return {k: 3 for k in kinds}


# ── slot_for ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("h", "mi", "expected"),
    [
        (7, 0, "manana"), (12, 59, "manana"), (13, 0, "tarde"), (20, 59, "tarde"),
        (21, 0, "noche"), (23, 30, "noche"), (0, 0, "noche"), (3, 15, "noche"), (6, 59, "noche"),
    ],
)
def test_slot_for_midnight_crossing(h: int, mi: int, expected: str) -> None:
    sched = Scheduler(madrid_grid())
    assert sched.slot_for(local(2026, 3, 10, h, mi)).name == expected


def test_slot_for_converts_utc_to_madrid_dst_safe() -> None:
    sched = Scheduler(madrid_grid())
    # Invierno (UTC+1): 20:30 UTC = 21:30 Madrid → noche
    assert sched.slot_for(datetime(2026, 1, 15, 20, 30, tzinfo=UTC)).name == "noche"
    # Verano (UTC+2): 19:30 UTC = 21:30 Madrid → noche; sin conversión sería tarde
    assert sched.slot_for(datetime(2026, 7, 15, 19, 30, tzinfo=UTC)).name == "noche"
    # Día del cambio de hora (29/03/2026 a las 01:00 UTC): 05:30 UTC = 07:30 Madrid
    assert sched.slot_for(datetime(2026, 3, 29, 5, 30, tzinfo=UTC)).name == "manana"
    # Justo antes del cambio: 00:30 UTC = 01:30 Madrid (CET)
    assert sched.slot_for(datetime(2026, 3, 29, 0, 30, tzinfo=UTC)).name == "noche"


def test_slot_for_default_when_no_slots() -> None:
    slot = Scheduler(GridConfig()).slot_for(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert slot.name == "default"
    assert slot.start == "00:00" and slot.end == "00:00"


def test_naive_datetime_treated_as_local() -> None:
    sched = Scheduler(madrid_grid())
    assert sched.slot_for(datetime(2026, 7, 15, 21, 30)).name == "noche"


# ── Regla 1: disponibilidad ──────────────────────────────────────────────────

def test_none_when_nothing_available() -> None:
    sched = Scheduler(madrid_grid(), random.Random(1))
    assert sched.next_kind(local(2026, 3, 10, 10, 30), [], {}) is None
    assert sched.next_kind(local(2026, 3, 10, 10, 30), [], {"music": 0}) is None
    assert "nada" in sched.explain()


def test_never_returns_unavailable_kind() -> None:
    now = local(2026, 3, 10, 10, 2)
    for seed in range(50):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        avail = {"music": 0, "factual": 1, "fiction": 0, "time_signal": 0}
        assert sched.next_kind(now, [], avail) == "factual"


# ── Regla 2: señal horaria ───────────────────────────────────────────────────

def test_time_signal_at_top_of_hour() -> None:
    sched = Scheduler(madrid_grid(), random.Random(0))
    now = local(2026, 3, 10, 10, 3)
    history = [rec("factual", now - timedelta(seconds=90))]  # racha y presupuesto excedidos
    assert sched.next_kind(now, history, ALL) == "time_signal"
    assert "señal horaria" in sched.explain()


def test_time_signal_not_after_minute_5() -> None:
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(local(2026, 3, 10, 10, 5), [], ALL) != "time_signal"


def test_time_signal_respects_cooldown() -> None:
    sched = Scheduler(madrid_grid(), random.Random(0))
    now = local(2026, 3, 10, 10, 4)
    history = [rec("time_signal", local(2026, 3, 10, 10, 0))]
    assert sched.next_kind(now, history, ALL) != "time_signal"


def test_time_signal_disabled() -> None:
    sched = Scheduler(madrid_grid(time_signal_enabled=False), random.Random(0))
    assert sched.next_kind(local(2026, 3, 10, 10, 1), [], ALL) != "time_signal"


def test_time_signal_uses_local_minute() -> None:
    # India (UTC+5:30): 10:32 UTC = 16:02 local → señal horaria
    sched = Scheduler(madrid_grid(timezone="Asia/Kolkata"), random.Random(0))
    assert sched.next_kind(datetime(2026, 3, 10, 10, 32, tzinfo=UTC), [], ALL) == "time_signal"


# ── Regla 3: cooldowns ───────────────────────────────────────────────────────

def test_factual_cooldown_blocks() -> None:
    now = local(2026, 3, 10, 10, 30)
    history = [
        rec("factual", now - timedelta(minutes=14)),
        rec("music", now - timedelta(minutes=12), 720),
    ]
    for seed in range(30):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        assert sched.next_kind(now, history, only("music", "factual")) == "music"


def test_factual_allowed_after_cooldown() -> None:
    now = local(2026, 3, 10, 10, 30)
    history = [
        rec("factual", now - timedelta(minutes=16)),
        rec("music", now - timedelta(minutes=14), 840),
    ]
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(now, history, only("music", "factual")) == "factual"


def test_fiction_and_jingle_cooldowns() -> None:
    now = local(2026, 3, 10, 10, 30)
    history = [
        rec("fiction", now - timedelta(minutes=29)),
        rec("jingle", now - timedelta(minutes=9)),
        rec("music", now - timedelta(minutes=8), 480),
    ]
    for seed in range(30):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        assert sched.next_kind(now, history, only("music", "fiction", "jingle")) == "music"


# ── Regla 4: racha de palabra ────────────────────────────────────────────────

def test_talk_run_limit_forces_music() -> None:
    # Tarde: max_talk_run_min=3 → 180 s. Historial con mucha música previa (presupuesto OK).
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("music", now - timedelta(minutes=60), 3600 - 180),
        rec("host_intro", now - timedelta(seconds=180)),
        rec("jingle", now - timedelta(seconds=90), 0.1),  # transparente
        rec("host_intro", now - timedelta(seconds=90)),
    ]
    for seed in range(30):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        assert sched.next_kind(now, history, only("music", "factual", "fiction")) == "music"
    assert "racha" in sched.explain()


def test_talk_run_under_limit_allows_talk() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("music", now - timedelta(minutes=60), 3600 - 90),
        rec("host_intro", now - timedelta(seconds=90)),
    ]
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(now, history, only("music", "factual")) == "factual"


# ── Regla 5: presupuesto de palabra ──────────────────────────────────────────

def test_empty_history_allows_talk() -> None:
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(local(2026, 3, 10, 15, 30), [], only("music", "factual")) == "factual"


def test_budget_exceeded_blocks_talk() -> None:
    # Tarde: presupuesto min(0.30, 0.30)=0.30. Última hora: 20 min palabra de 60 → 0.33.
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("factual", now - timedelta(minutes=60), 20 * 60),
        rec("music", now - timedelta(minutes=40), 40 * 60),
    ]
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(now, history, only("music", "factual")) == "music"
    assert "presupuesto" in sched.explain()


def test_budget_uses_slot_music_ratio() -> None:
    # Noche: min(0.30, 1-0.80)=0.20. Ratio 0.25 permitido en tarde, bloqueado en noche.
    def history_at(now: datetime) -> list[PlayRecord]:
        return [
            rec("factual", now - timedelta(minutes=60), 15 * 60),
            rec("music", now - timedelta(minutes=45), 45 * 60),
        ]

    tarde = local(2026, 3, 10, 15, 30)
    noche = local(2026, 3, 10, 23, 30)
    s1 = Scheduler(madrid_grid(), random.Random(0))
    s2 = Scheduler(madrid_grid(), random.Random(0))
    assert s1.next_kind(tarde, history_at(tarde), only("music", "factual")) == "factual"
    assert s2.next_kind(noche, history_at(noche), only("music", "factual")) == "music"


def test_budget_ignores_records_outside_window() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("factual", now - timedelta(minutes=120), 3600),  # fuera de la ventana
        rec("music", now - timedelta(minutes=60), 3600),
    ]
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(now, history, only("music", "factual")) == "factual"


# ── Regla 6: preferencias ────────────────────────────────────────────────────

def test_talk_preference_factual_most_frequent() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("music", now - timedelta(minutes=60), 3600 - 90),
        rec("host_intro", now - timedelta(seconds=90)),  # sin boost de host_intro
    ]
    counts: dict[str | None, int] = {}
    avail = only("music", "factual", "fiction")
    for seed in range(300):
        k = Scheduler(madrid_grid(), random.Random(seed)).next_kind(now, history, avail)
        counts[k] = counts.get(k, 0) + 1
    assert counts.get("factual", 0) > counts.get("fiction", 0) > 0


def test_host_intro_favoured_after_music_when_quiet() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [rec("music", now - timedelta(minutes=60), 3600)]
    avail = only("music", "factual", "host_intro", "fiction")
    counts: dict[str | None, int] = {}
    for seed in range(300):
        k = Scheduler(madrid_grid(), random.Random(seed)).next_kind(now, history, avail)
        counts[k] = counts.get(k, 0) + 1
    assert counts.get("host_intro", 0) > counts.get("factual", 0)


def test_host_intro_never_twice_in_a_row() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("music", now - timedelta(minutes=60), 3600 - 60),
        rec("host_intro", now - timedelta(seconds=60), 60),
    ]
    for seed in range(30):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        assert sched.next_kind(now, history, only("music", "host_intro")) == "music"


def test_jingle_occasionally_after_music() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [rec("music", now - timedelta(minutes=4), 240)]
    kinds = {
        Scheduler(madrid_grid(), random.Random(seed)).next_kind(now, history, only("music", "jingle"))
        for seed in range(100)
    }
    assert kinds == {"music", "jingle"}


def test_no_jingle_when_last_is_talk() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [
        rec("music", now - timedelta(minutes=60), 3600 - 90),
        rec("factual", now - timedelta(seconds=90)),
    ]
    for seed in range(50):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        assert sched.next_kind(now, history, only("music", "jingle")) == "music"


# ── Regla 7: valores por defecto ─────────────────────────────────────────────

def test_default_music() -> None:
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(local(2026, 3, 10, 15, 30), [], only("music")) == "music"


def test_fallback_breaks_budget_rather_than_dead_air() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [rec("factual", now - timedelta(minutes=5), 300)]  # ratio 1.0 y racha > límite
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(now, history, only("fiction")) == "fiction"
    assert "sin música" in sched.explain()


def test_fallback_ignores_cooldown_as_last_resort() -> None:
    now = local(2026, 3, 10, 15, 30)
    history = [rec("factual", now - timedelta(minutes=5), 90)]
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(now, history, only("factual")) == "factual"
    assert "ignorando" in sched.explain()


def test_fallback_time_signal_outside_top_of_hour() -> None:
    sched = Scheduler(madrid_grid(), random.Random(0))
    assert sched.next_kind(local(2026, 3, 10, 15, 30), [], only("time_signal")) == "time_signal"


# ── Regla 8: determinismo ────────────────────────────────────────────────────

def simulate(
    start: datetime, hours: float, seed: int, grid: GridConfig | None = None
) -> tuple[list[PlayRecord], list[str]]:
    sched = Scheduler(grid or madrid_grid(), random.Random(seed))
    history: list[PlayRecord] = []
    reasons: list[str] = []
    now = start
    end = start + timedelta(hours=hours)
    while now < end:
        kind = sched.next_kind(now, history, ALL)
        assert kind is not None
        reasons.append(sched.explain())
        history.append(rec(kind, now))
        now += timedelta(seconds=DURATIONS[kind])
    return history, reasons


def test_deterministic_with_seed() -> None:
    start = local(2026, 3, 10, 9, 17)
    a = simulate(start, 2, seed=42)
    b = simulate(start, 2, seed=42)
    assert a == b
    c = simulate(start, 2, seed=43)
    assert [r.kind for r in a[0]] != [r.kind for r in c[0]]


# ── Propiedad: simulación de 3 horas ─────────────────────────────────────────

def _talk_in_window(history: list[PlayRecord], w_start: datetime, w_end: datetime) -> float:
    talk = 0.0
    for r in history:
        if r.kind not in TALK_KINDS:
            continue
        s = r.started_at
        e = s + timedelta(seconds=r.duration_s)
        talk += max(0.0, (min(e, w_end) - max(s, w_start)).total_seconds())
    return talk


@pytest.mark.parametrize(
    ("start", "budget"),
    [
        (local(2026, 3, 10, 13, 20), 0.30),   # tarde
        (local(2026, 3, 10, 8, 40), 0.30),    # mañana: min(0.30, 0.35)
        (local(2026, 3, 10, 21, 10), 0.20),   # noche: min(0.30, 0.20)
    ],
)
@pytest.mark.parametrize("seed", [0, 1, 2, 7])
def test_simulation_three_hours(start: datetime, budget: float, seed: int) -> None:
    history, _ = simulate(start, 3, seed=seed)
    kinds = {r.kind for r in history}
    assert "music" in kinds and kinds & {"factual", "host_intro", "fiction"}

    # Presupuesto en cada hora móvil completa (una ventana por cada fin de segmento)
    tolerance = 0.04
    for r in history:
        w_end = r.started_at + timedelta(seconds=r.duration_s)
        w_start = w_end - timedelta(hours=1)
        if w_start < start:
            continue
        ratio = _talk_in_window(history, w_start, w_end) / 3600.0
        assert ratio <= budget + tolerance, (w_end, ratio)

    # Una señal horaria en cada hora en punto cubierta por la simulación
    end = start + timedelta(hours=3)
    hour = (start + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while hour + timedelta(minutes=5) <= end:
        signals = [
            r for r in history
            if r.kind == "time_signal" and hour <= r.started_at < hour + timedelta(minutes=5)
        ]
        assert len(signals) == 1, hour
        hour += timedelta(hours=1)

    # Ninguna racha de palabra (sin contar la señal horaria) supera el límite de la franja
    sched = Scheduler(madrid_grid())
    run = 0.0
    for r in history:
        if r.kind == "jingle":
            continue
        if r.kind in TALK_KINDS:
            limit = sched.slot_for(r.started_at).max_talk_run_min * 60
            if r.kind != "time_signal":
                assert run < limit
            run += r.duration_s
        else:
            run = 0.0


def test_host_intro_cooldown_blocks() -> None:
    """host_intro tiene su propio cooldown (grid.cooldowns_minutes.host_intro)."""
    now = local(2026, 3, 10, 10, 30)
    history = [
        rec("host_intro", now - timedelta(minutes=10), 20),
        rec("music", now - timedelta(minutes=9), 540),
    ]
    for seed in range(30):
        sched = Scheduler(madrid_grid(), random.Random(seed))
        assert sched.next_kind(now, history, only("music", "host_intro")) == "music"
    later = now + timedelta(minutes=3)
    picks = {
        Scheduler(madrid_grid(), random.Random(s)).next_kind(later, history, only("music", "host_intro"))
        for s in range(30)
    }
    assert "host_intro" in picks
