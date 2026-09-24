"""
Utilidades para los tests de la parrilla: segmentos sintéticos, historial y un bucle
de emisión puro (sin BD) sobre ``next_unit`` + ``advance_state``.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from radio.core.config import GridConfig
from radio.core.models import PlayLogEntry, Segment, StockView
from radio.grid.rules import hour_tag
from radio.grid.scheduler import PlayUnit, SchedulerState, advance_state, next_unit

MADRID = ZoneInfo("Europe/Madrid")
CREATED = datetime(2026, 1, 1, tzinfo=MADRID)
EMERGENCY_S = 30.0

# Parrilla del ejemplo de §5 (más cooldown de jingle y huecos jingle como config/grid.yaml)
DOC_GRID: dict[str, Any] = {
    "timezone": "Europe/Madrid",
    "talk_budget": {"window_minutes": 60, "max_ratio": 0.22},
    "cooldowns_minutes": {"consultorio": 90, "horoscope": 720, "weather": 180},
    "modes": {
        "default": {
            "interrupts": [
                {"kind": "time_signal", "when": "minute == 0", "max_late_seconds": 90},
            ],
            "dayparts": [
                {"name": "manana", "from": "07:00", "to": "12:00",
                 "pattern": ["music", "talk", "music", "music"],
                 "talk_pool": {"weather": 3, "ephemeris": 2, "horoscope": 2, "word_of_day": 1}},
                {"name": "tarde", "from": "12:00", "to": "20:00",
                 "pattern": ["music", "music", "talk"],
                 "talk_pool": {"consultorio": 3, "liga": 2, "artist_fact": 2, "trivia": 1}},
                {"name": "noche", "from": "20:00", "to": "07:00",
                 "pattern": ["music", "music", "music", "talk"],
                 "talk_pool": {"radionovela": 3, "interview": 2}},
            ],
        },
        "tinydesk": {
            "dayparts": [{"name": "todo", "from": "00:00", "to": "24:00",
                          "pattern": ["music"], "talk_pool": {}}],
        },
    },
}

FACTUAL_KINDS = frozenset({
    "weather", "ephemeris", "word_of_day", "artist_fact", "trivia", "time_signal",
    "host_intro", "news",
})


def doc_grid(**overrides: Any) -> GridConfig:
    data = {**DOC_GRID, **overrides}
    return GridConfig.model_validate(data)


def simple_grid(pattern: list[str], pool: dict[str, float] | None = None, *,
                interrupts: bool = False, **overrides: Any) -> GridConfig:
    mode: dict[str, Any] = {"dayparts": [{"name": "todo", "from": "00:00", "to": "24:00",
                                          "pattern": pattern, "talk_pool": pool or {}}]}
    if interrupts:
        mode["interrupts"] = [{"kind": "time_signal", "when": "minute == 0",
                               "max_late_seconds": 90}]
    return GridConfig.model_validate(
        {"timezone": "Europe/Madrid", "modes": {"default": mode}, **overrides}
    )


def local(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=MADRID)


_counter = [0]


def seg(seg_id: str, kind: str = "music", duration: float = 200.0, *,
        factual: bool | None = None, tags: Iterable[str] = (), parent_id: str | None = None,
        priority: int = 0, expires_at: datetime | None = None, status: str = "ready",
        order: int | None = None) -> Segment:
    if order is None:
        _counter[0] += 1
        order = _counter[0]
    return Segment(
        id=seg_id, kind=kind,
        factual=(kind in FACTUAL_KINDS) if factual is None else factual,
        path=Path(f"/t/{seg_id}.mp3"), duration_s=duration,
        created_at=CREATED + timedelta(microseconds=order), producer="test",
        status=status,  # type: ignore[arg-type]
        expires_at=expires_at, priority=priority, parent_id=parent_id,
        meta={"title": seg_id, "tags": list(tags)},
    )


def signal(hour: datetime, seg_id: str | None = None) -> Segment:
    """Señal horaria de la hora local de ``hour`` (caduca 5 min después)."""
    top = hour.astimezone(MADRID).replace(minute=0, second=0, microsecond=0)
    return seg(seg_id or f"ts-{top:%Y%m%d%H}", "time_signal", 3.0,
               tags=[hour_tag(top)], priority=1, expires_at=top + timedelta(minutes=5))


def stock(segments: Iterable[Segment], now: datetime) -> StockView:
    return StockView.from_segments(segments, now)


def entry(s: Segment, start: datetime, duration: float | None = None, i: int = 1) -> PlayLogEntry:
    d = s.duration_s if duration is None else duration
    return PlayLogEntry(id=i, segment_id=s.id, kind=s.kind, mode="default",
                        started_at=start, ended_at=start + timedelta(seconds=d),
                        skipped=False, duration_s=d)


def state_after(grid: GridConfig, played: Iterable[tuple[Segment, datetime]],
                **kw: Any) -> SchedulerState:
    """Estado con ese historial (segmento, inicio) y los segmentos conocidos."""
    pairs = list(played)
    return SchedulerState(
        grid=grid,
        history=tuple(entry(s, t, i=n + 1) for n, (s, t) in enumerate(pairs)),
        segments={s.id: s for s, _ in pairs},
        **kw,
    )


def music_catalog(n: int = 60, artists: int = 12, durations: tuple[float, float] = (150, 600),
                  seed: int = 0) -> list[Segment]:
    rng = random.Random(seed)
    return [
        seg(f"m{i:03d}", "music", round(rng.uniform(*durations), 1),
            tags=[f"artist:a{i % artists}"], order=i)
        for i in range(n)
    ]


# ── Bucle puro ────────────────────────────────────────────────────────────────

@dataclass
class Aired:
    seg: Segment
    start: datetime
    end: datetime
    unit_index: int


@dataclass
class PureRun:
    units: list[tuple[datetime, PlayUnit]] = field(default_factory=list)
    aired: list[Aired] = field(default_factory=list)
    emergency_s: float = 0.0
    end: datetime | None = None


def run_pure(grid: GridConfig, segments: Iterable[Segment], start: datetime, hours: float,
             seed: int, mode: str = "default", signals: bool = True) -> PureRun:
    """
    Emite ``hours`` horas desde ``start`` sin BD: pide unidades, las "emite" (el reloj
    avanza su duración), retira la palabra (como la emisora) y añade señales horarias.
    """
    rng = random.Random(seed)
    pool = {s.id: s for s in segments}
    if signals:
        top = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        for k in range(int(hours) + 2):
            ts = signal((top + timedelta(hours=k)).astimezone(MADRID))
            pool[ts.id] = ts
    state = SchedulerState(grid=grid)
    now = start.astimezone(UTC)   # aritmética en UTC: con la zona local sería hora de pared
    end = start + timedelta(hours=hours)
    run = PureRun()
    while now < end:
        unit = next_unit(state, stock(pool.values(), now), now, mode, rng)
        run.units.append((now, unit))
        if unit.is_emergency:
            now += timedelta(seconds=EMERGENCY_S)
            run.emergency_s += EMERGENCY_S
            continue
        t = now
        for s in unit.segments:
            run.aired.append(Aired(s, t, t + timedelta(seconds=s.duration_s), len(run.units) - 1))
            t += timedelta(seconds=s.duration_s)
            if s.kind not in ("music", "jingle", "stinger"):
                pool[s.id] = replace(s, status="retired")
        state = advance_state(state, unit, now)
        now = t
    run.end = now
    return run


def max_talk_ratio(run: PureRun, origin: datetime, window_min: float = 60) -> float:
    """Máxima proporción de palabra en ventanas que terminan al final de cada segmento."""
    window = timedelta(minutes=window_min)
    best = 0.0
    for a in run.aired:
        if a.end - window < origin:
            continue
        talk = sum(
            max(0.0, (min(b.end, a.end) - max(b.start, a.end - window)).total_seconds())
            for b in run.aired if b.seg.kind not in ("music", "jingle", "stinger")
        )
        best = max(best, talk / window.total_seconds())
    return best


def fiction_after_factual(run: PureRun) -> int:
    count = 0
    prev_factual = False
    for a in run.aired:
        if a.seg.kind in ("music", "jingle", "stinger"):
            prev_factual = False
            continue
        if prev_factual and not a.seg.factual:
            count += 1
        prev_factual = a.seg.factual
    return count
