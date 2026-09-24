"""
Tests del scheduler de parrilla (radio.grid.scheduler): propiedades de §9,
interrupciones, patrón, vinculación y cada peldaño de la escalera de §8.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from radio.core.config import GridConfig
from radio.core.models import Segment, StockView
from radio.grid.budget import talk_ratio
from radio.grid.scheduler import (
    PlayUnit,
    SchedulerState,
    advance_state,
    history_horizon,
    next_unit,
)
from tests.fixtures.grid import (
    doc_grid,
    fiction_after_factual,
    local,
    max_talk_ratio,
    music_catalog,
    run_pure,
    seg,
    signal,
    simple_grid,
    state_after,
    stock,
)

NOON = local(2026, 1, 5, 12, 20)       # franja "tarde" del ejemplo de §5
MORNING = local(2026, 1, 5, 9, 20)     # franja "manana"


def unit_at(grid: GridConfig, segs: list[Segment], now: datetime, *,
            state: SchedulerState | None = None, mode: str = "default",
            seed: int = 0) -> PlayUnit:
    return next_unit(state or SchedulerState(grid=grid), stock(segs, now), now, mode,
                     random.Random(seed))


def ids(unit: PlayUnit) -> list[str]:
    return [s.id for s in unit.segments]


def talk_stock(n: int = 30) -> list[Segment]:
    """Palabra de todos los kinds del ejemplo de §5 (factual y ficción)."""
    kinds = ["weather", "ephemeris", "horoscope", "word_of_day", "consultorio", "liga",
             "artist_fact", "trivia", "radionovela", "interview"]
    rng = random.Random(3)
    return [seg(f"{k}-{i}", k, round(rng.uniform(40, 120), 1)) for k in kinds for i in range(n)]


# ── Propiedades (§9) ──────────────────────────────────────────────────────────

def test_never_returns_none_or_inconsistent_unit() -> None:
    grid = doc_grid()
    everything = music_catalog(20) + talk_stock(3) + [seg("j", "jingle", 8.0)]
    rng = random.Random(42)
    for i in range(300):
        now = local(2026, 1, 5) + timedelta(minutes=rng.randrange(0, 48 * 60))
        subset = [s for s in everything if rng.random() < rng.random()]
        played = [(s, now - timedelta(minutes=rng.randrange(1, 300)))
                  for s in rng.sample(everything, rng.randrange(0, 10))]
        played.sort(key=lambda p: p[1])
        unit = unit_at(grid, subset, now, state=state_after(grid, played),
                       mode=rng.choice(["default", "tinydesk", "desconocido"]), seed=i)
        assert isinstance(unit, PlayUnit)
        assert 1 <= unit.rung <= 5
        assert unit.is_emergency == (unit.rung == 5)
        assert unit.reason


def test_empty_stock_is_rung_5() -> None:
    unit = unit_at(doc_grid(), [], NOON)
    assert unit.rung == 5 and unit.segments == () and unit.is_emergency


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_never_over_talk_budget(seed: int) -> None:
    # Patrón muy hablador y música corta: el presupuesto es lo único que frena
    grid = simple_grid(["talk", "talk", "music"],
                       {"weather": 1, "ephemeris": 1, "consultorio": 1, "liga": 1},
                       interrupts=True)
    start = local(2026, 1, 5, 6, 17)
    run = run_pure(grid, music_catalog(80, durations=(60, 150), seed=seed) + talk_stock(),
                   start, 12, seed)
    assert run.emergency_s == 0
    assert any(a.seg.kind == "consultorio" for a in run.aired)
    ratio = max_talk_ratio(run, start)
    assert 0.15 < ratio <= 0.22 + 1e-9, ratio


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_never_fiction_right_after_factual(seed: int) -> None:
    # Sin música entre huecos de palabra: la única protección es la regla §1.4
    grid = simple_grid(["talk", "talk", "talk", "music"],
                       {"weather": 1, "consultorio": 1, "radionovela": 1},
                       talk_budget={"window_minutes": 60, "max_ratio": 1.0})
    start = local(2026, 1, 5, 10)
    run = run_pure(grid, music_catalog(30) + talk_stock(), start, 8, seed, signals=False)
    assert fiction_after_factual(run) == 0
    kinds = [a.seg.kind for a in run.aired]
    assert "consultorio" in kinds and "weather" in kinds


def test_fiction_blocked_after_factual_until_music_or_jingle() -> None:
    grid = simple_grid(["talk"], {"consultorio": 1})
    weather = seg("w", "weather", 60.0)
    cons = seg("c", "consultorio", 60.0)
    music = seg("m", "music", 200.0)
    jingle = seg("j", "jingle", 8.0)
    after_factual = state_after(grid, [(weather, NOON - timedelta(minutes=1))])
    unit = unit_at(grid, [cons, music], NOON, state=after_factual)
    assert ids(unit) == ["m"] and unit.rung == 3
    # Sin música tampoco se rompe la regla: emergencia antes que ficción pegada
    assert unit_at(grid, [cons], NOON, state=after_factual).rung == 5
    # Con música o jingle entre medias, sí
    for sep in (music, jingle):
        st = state_after(grid, [(weather, NOON - timedelta(minutes=5)),
                                (sep, NOON - timedelta(minutes=4))])
        assert ids(unit_at(grid, [cons, music], NOON, state=st)) == ["c"]


def test_unknown_previous_segment_is_assumed_factual() -> None:
    grid = simple_grid(["talk"], {"consultorio": 1})
    ghost = seg("ghost", "weather", 60.0)
    st = state_after(grid, [(ghost, NOON - timedelta(minutes=1))])
    st = SchedulerState(grid=grid, history=st.history)       # sin ``segments``
    unit = unit_at(grid, [seg("c", "consultorio", 60.0), seg("m", "music")], NOON, state=st)
    assert ids(unit) == ["m"]


def test_respects_cooldowns() -> None:
    grid = doc_grid()
    weather = [seg(f"w{i}", "weather", 60.0) for i in range(3)]
    eph = seg("e", "ephemeris", 60.0)
    music = seg("m", "music", 200.0)
    st = state_after(grid, [(weather[0], MORNING - timedelta(minutes=120)),
                            (music, MORNING - timedelta(minutes=110))],
                     pattern_pos={"default/manana": 1})       # hueco talk
    for s in range(20):
        unit = unit_at(grid, [*weather[1:], eph, music], MORNING, state=st, seed=s)
        assert ids(unit) == ["e"] and unit.rung == 1
    # Pasado el cooldown (180 min) vuelve a salir weather (peso 3 contra 2)
    later = MORNING + timedelta(minutes=61)
    chosen = {ids(unit_at(grid, [*weather[1:], eph, music], later, state=st, seed=s))[0]
              for s in range(20)}
    assert chosen == {"w1", "e"}


@pytest.mark.parametrize("seed", [5, 6])
def test_cooldowns_only_broken_by_relaxed_rungs(seed: int) -> None:
    grid = doc_grid(cooldowns_minutes={"weather": 180, "consultorio": 90, "radionovela": 120})
    start = local(2026, 1, 5, 6)
    run = run_pure(grid, music_catalog(80, seed=seed) + talk_stock(), start, 24, seed)
    last: dict[str, datetime] = {}
    for a in run.aired:
        limit = grid.cooldowns_minutes.get(a.seg.kind)
        rung = run.units[a.unit_index][1].rung
        if limit and a.seg.kind in last and rung == 1:
            assert a.start - last[a.seg.kind] >= timedelta(minutes=limit), a.seg.kind
        last[a.seg.kind] = a.start


def test_deterministic_with_same_seed() -> None:
    grid = doc_grid()
    start = local(2026, 1, 5, 11, 30)
    segs = music_catalog(60) + talk_stock(5) + [seg("j", "jingle", 8.0)]

    def timeline(seed: int) -> list[tuple[datetime, str]]:
        return [(a.start, a.seg.id) for a in run_pure(grid, segs, start, 6, seed).aired]

    assert timeline(9) == timeline(9)
    assert timeline(9) != timeline(10)


# ── Interrupciones ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("offset_s", "airs"), [(0, True), (45, True), (90, True), (91, False)])
def test_time_signal_within_max_late_seconds(offset_s: int, airs: bool) -> None:
    grid = doc_grid()
    now = local(2026, 1, 5, 11) + timedelta(seconds=offset_s)
    ts = signal(local(2026, 1, 5, 11))
    unit = unit_at(grid, [ts, seg("m", "music")], now)
    assert (ids(unit) == [ts.id]) is airs
    assert unit.interrupt is airs
    if airs:
        assert unit.pattern_key is None and unit.rung == 1


def test_time_signal_needs_matching_hour_tag() -> None:
    grid = doc_grid()
    now = local(2026, 1, 5, 11, 0, 10)
    wrong = signal(local(2026, 1, 5, 12))
    assert ids(unit_at(grid, [wrong, seg("m", "music")], now)) == ["m"]
    untagged = seg("ts-generic", "time_signal", 3.0, priority=1)
    assert ids(unit_at(grid, [untagged, seg("m", "music")], now)) == ["ts-generic"]


def test_time_signal_only_once_per_hour() -> None:
    grid = doc_grid()
    ts = signal(local(2026, 1, 5, 11))
    other = seg("ts-dup", "time_signal", 3.0, tags=ts.tags, priority=1)
    st = state_after(grid, [(ts, local(2026, 1, 5, 11, 0, 5))])
    unit = unit_at(grid, [other, seg("m", "music")], local(2026, 1, 5, 11, 0, 20), state=st)
    assert ids(unit) == ["m"]


def test_time_signal_ignores_budget_and_pattern() -> None:
    grid = doc_grid(talk_budget={"window_minutes": 60, "max_ratio": 0.0})
    ts = signal(local(2026, 1, 5, 11))
    unit = unit_at(grid, [ts, seg("m", "music")], local(2026, 1, 5, 11, 0, 30))
    assert ids(unit) == [ts.id]


def test_tinydesk_has_no_time_signal() -> None:
    grid = doc_grid()
    ts = signal(local(2026, 1, 5, 11))
    unit = unit_at(grid, [ts, seg("m", "music")], local(2026, 1, 5, 11), mode="tinydesk")
    assert ids(unit) == ["m"]


def test_priority_segment_interrupts_when_allowed() -> None:
    grid = doc_grid()
    urgent = seg("u", "weather", 30.0, priority=2)
    normal = seg("w", "weather", 30.0)
    unit = unit_at(grid, [normal, urgent, seg("m", "music")], NOON)   # tarde: weather no está en pool
    assert ids(unit) == ["u"] and unit.interrupt and unit.pattern_key is None
    # En tinydesk (solo música) no entra
    assert ids(unit_at(grid, [urgent, seg("m", "music")], NOON, mode="tinydesk")) == ["m"]


def test_signal_at_dst_repeated_hour() -> None:
    grid = doc_grid()
    # 25-10-2026 02:00 CET (fold=1) = 01:00 UTC; la etiqueta es la misma que la de 02:00 CEST
    first = datetime(2026, 10, 25, 0, 0, 10, tzinfo=UTC)
    ts = signal(first)
    assert ids(unit_at(grid, [ts, seg("m", "music")], first)) == [ts.id]
    st = state_after(grid, [(ts, first)])
    retired = seg(ts.id, "time_signal", 3.0, tags=ts.tags, status="retired")
    second = datetime(2026, 10, 25, 1, 0, 10, tzinfo=UTC)
    unit = unit_at(grid, [retired, seg("m", "music")], second, state=st)
    assert ids(unit) == ["m"]


# ── Preferencia horaria y relleno ─────────────────────────────────────────────

def test_music_prefers_ending_before_next_signal() -> None:
    grid = doc_grid()
    now = local(2026, 1, 5, 10, 50)
    segs = [seg("long", "music", 900.0), seg("short", "music", 200.0),
            signal(local(2026, 1, 5, 11))]
    for s in range(10):
        assert ids(unit_at(grid, segs, now, seed=s)) == ["short"]
    # Sin señal preparada no hay preferencia
    chosen = {ids(unit_at(grid, segs[:2], now, seed=s))[0] for s in range(20)}
    assert chosen == {"long", "short"}


def test_music_avoids_leaving_unfillable_gap() -> None:
    grid = doc_grid()
    now = local(2026, 1, 5, 10, 52)                 # quedan 480 s
    segs = [seg("gap", "music", 400.0),             # dejaría 80 s: no cabe nada
            seg("fits", "music", 300.0),            # deja 180 s: cabe "tiny"
            seg("tiny", "music", 150.0),
            signal(local(2026, 1, 5, 11))]
    chosen = {ids(unit_at(grid, segs, now, seed=s))[0] for s in range(30)}
    assert "gap" not in chosen


def test_jingle_filler_when_no_music_fits() -> None:
    grid = doc_grid()
    now = local(2026, 1, 5, 10, 59, 30)             # 30 s + 90 s de margen
    segs = [seg("m", "music", 200.0), seg("j", "jingle", 8.0),
            signal(local(2026, 1, 5, 11))]
    unit = unit_at(grid, segs, now)
    assert ids(unit) == ["j"] and unit.pattern_key is None and "relleno" in unit.reason
    # En tinydesk (sin interrupciones) no hay relleno
    assert ids(unit_at(grid, segs, now, mode="tinydesk")) == ["m"]


def test_no_jingle_chain_before_a_distant_interrupt() -> None:
    """Conciertos largos: a 10 min de la señal no se encadenan jingles; suena música
    (la emisora la cortará en punto si ``cut_music``)."""
    grid = doc_grid()
    now = local(2026, 1, 5, 10, 50)
    segs = [seg("concierto", "music", 1500.0), seg("j", "jingle", 8.0),
            signal(local(2026, 1, 5, 11))]
    unit = unit_at(grid, segs, now)
    assert ids(unit) == ["concierto"] and "ninguna acaba antes" in unit.reason


# ── Patrón y franjas ──────────────────────────────────────────────────────────

def test_pattern_cycles_with_advance_state() -> None:
    grid = simple_grid(["music", "talk", "jingle"], {"weather": 1})
    segs = music_catalog(10) + [seg(f"w{i}", "weather", 30.0) for i in range(5)] + [
        seg("j1", "jingle", 8.0), seg("j2", "jingle", 8.0)]
    state = SchedulerState(grid=grid)
    now = NOON
    slots = []
    for i in range(7):
        unit = next_unit(state, stock(segs, now), now, "default", random.Random(i))
        slots.append(unit.slot)
        state = advance_state(state, unit, now)
        now += timedelta(seconds=unit.duration_s)
    assert slots == ["music", "talk", "jingle", "music", "talk", "jingle", "music"]
    assert state.pattern_pos == {"default/todo": 7}


def test_pattern_cursor_is_per_daypart() -> None:
    grid = doc_grid()
    st = SchedulerState(grid=grid, pattern_pos={"default/tarde": 2, "default/manana": 0})
    segs = music_catalog(5) + [seg("c", "consultorio", 60.0)]
    assert unit_at(grid, segs, NOON, state=st).slot == "talk"      # tarde[2]
    assert unit_at(grid, segs, MORNING, state=st).slot == "music"  # manana[0]


def test_tinydesk_mode_is_music_only() -> None:
    grid = doc_grid()
    segs = music_catalog(40) + talk_stock(5) + [seg("j", "jingle", 8.0)]
    segs.append(seg("intro", "host_intro", 20.0, parent_id="m000"))
    run = run_pure(grid, segs, local(2026, 1, 5, 6), 12, 1, mode="tinydesk")
    assert {a.seg.kind for a in run.aired} <= {"music", "host_intro"}
    assert run.emergency_s == 0


def test_talk_slot_with_empty_pool_is_music() -> None:
    grid = simple_grid(["talk"], {})
    unit = unit_at(grid, [seg("m", "music"), seg("w", "weather", 30.0)], NOON)
    assert ids(unit) == ["m"] and unit.rung == 1 and unit.slot == "talk"


def test_talk_budget_turns_talk_slot_into_music() -> None:
    grid = simple_grid(["talk"], {"weather": 1})
    long_talk = seg("old", "weather", 800.0)
    st = state_after(grid, [(long_talk, NOON - timedelta(minutes=14))])
    assert talk_ratio(st.history, NOON, timedelta(minutes=60)) >= 0.22
    unit = unit_at(grid, [seg("w", "weather", 30.0), seg("m", "music")], NOON, state=st)
    assert ids(unit) == ["m"] and unit.rung == 1 and "presupuesto" in unit.reason


def test_no_talk_candidate_fits_budget_turns_into_music() -> None:
    grid = simple_grid(["talk"], {"weather": 1})
    st = state_after(grid, [(seg("old", "weather", 700.0), NOON - timedelta(minutes=30))])
    unit = unit_at(grid, [seg("w", "weather", 200.0), seg("m", "music")], NOON, state=st)
    assert ids(unit) == ["m"] and unit.rung == 1 and "cabe" in unit.reason


def test_weighted_talk_choice_follows_pool() -> None:
    grid = simple_grid(["talk"], {"weather": 9, "ephemeris": 1})
    segs = [seg("w", "weather", 30.0), seg("e", "ephemeris", 30.0)]
    picks = [ids(unit_at(grid, segs, NOON, seed=s))[0] for s in range(200)]
    assert picks.count("w") > 150 and picks.count("e") > 5


def test_talk_prefers_soonest_expiry() -> None:
    grid = simple_grid(["talk"], {"weather": 1})
    later = seg("later", "weather", 30.0, expires_at=NOON + timedelta(hours=5))
    soon = seg("soon", "weather", 30.0, expires_at=NOON + timedelta(hours=1))
    expired = seg("gone", "weather", 30.0, expires_at=NOON - timedelta(minutes=1))
    assert ids(unit_at(grid, [later, soon, expired], NOON)) == ["soon"]


# ── Música: repetición y artista ──────────────────────────────────────────────

def test_music_not_same_artist_nor_recent() -> None:
    grid = simple_grid(["music"])
    cat = music_catalog(12, artists=4)
    played = [(cat[i], NOON - timedelta(minutes=60 - 5 * i)) for i in range(5)]
    st = state_after(grid, played)
    last_artist = cat[4].tags[0]
    for s in range(30):
        unit = unit_at(grid, cat, NOON, state=st, seed=s)
        chosen = unit.segments[0]
        assert last_artist not in chosen.tags
        assert chosen.id not in {c.id for c, _ in played}
        assert unit.rung == 1


# ── Vinculación ───────────────────────────────────────────────────────────────

def test_music_links_its_host_intro() -> None:
    grid = simple_grid(["music"])
    m = seg("m", "music", 200.0)
    intro = seg("i", "host_intro", 20.0, parent_id="m")
    other = seg("i2", "host_intro", 20.0, parent_id="otra")
    unit = unit_at(grid, [m, intro, other], NOON)
    assert ids(unit) == ["i", "m"] and unit.duration_s == 220.0


def test_host_intro_never_airs_alone() -> None:
    grid = simple_grid(["talk"], {"host_intro": 5})
    unit = unit_at(grid, [seg("i", "host_intro", 20.0, parent_id="x")], NOON)
    assert unit.rung == 5


def test_host_intro_dropped_when_over_budget_or_aired() -> None:
    grid = simple_grid(["music"])
    m = seg("m", "music", 200.0)
    intro = seg("i", "host_intro", 20.0, parent_id="m")
    busy = state_after(grid, [(seg("t", "weather", 790.0), NOON - timedelta(minutes=14))])
    assert ids(unit_at(grid, [m, intro], NOON, state=busy)) == ["m"]
    aired = state_after(grid, [(intro, NOON - timedelta(minutes=30))])
    assert ids(unit_at(grid, [m, intro], NOON, state=aired)) == ["m"]


def test_non_factual_intro_respects_fiction_rule() -> None:
    grid = simple_grid(["music"])
    m = seg("m", "music", 200.0)
    intro = seg("i", "host_intro", 20.0, parent_id="m", factual=False)
    st = state_after(grid, [(seg("w", "weather", 30.0), NOON - timedelta(minutes=1))])
    assert ids(unit_at(grid, [m, intro], NOON, state=st)) == ["m"]


# ── Escalera de degradación (§8) ──────────────────────────────────────────────

def test_rung_1_ideal() -> None:
    unit = unit_at(simple_grid(["talk"], {"weather": 1}), [seg("w", "weather", 30.0)], NOON)
    assert unit.rung == 1 and ids(unit) == ["w"]


def test_rung_2_relaxes_cooldown() -> None:
    grid = simple_grid(["talk"], {"weather": 1}, cooldowns_minutes={"weather": 180})
    st = state_after(grid, [(seg("w0", "weather", 30.0), NOON - timedelta(minutes=30))])
    unit = unit_at(grid, [seg("w1", "weather", 30.0), seg("m", "music")], NOON, state=st)
    assert unit.rung == 2 and ids(unit) == ["w1"]


def test_rung_2_relaxes_music_repetition() -> None:
    grid = simple_grid(["music"])
    x1 = seg("x1", "music", tags=["artist:x"])
    ys = [seg(f"y{i}", "music", tags=["artist:y"]) for i in range(3)]
    # La única canción de otro artista acaba de sonar: solo sale relajando la repetición
    st = state_after(grid, [(x1, NOON - timedelta(minutes=20)),
                            (ys[0], NOON - timedelta(minutes=10))])
    unit = unit_at(grid, [x1, *ys], NOON, state=st)
    assert unit.rung == 2 and ids(unit) == ["x1"]


def test_rung_3_any_music() -> None:
    grid = simple_grid(["talk"], {"weather": 1})
    unit = unit_at(grid, [seg("m", "music")], NOON)
    assert unit.rung == 3 and ids(unit) == ["m"] and unit.pattern_key == "default/todo"


def test_rung_4_repeats_oldest_aired() -> None:
    grid = simple_grid(["talk"], {"weather": 1})
    w1 = seg("w1", "weather", 30.0, status="retired")
    w2 = seg("w2", "weather", 30.0, status="retired")
    bad = seg("q", "weather", 30.0, status="quarantined")
    old = seg("old", "weather", 30.0, status="retired", expires_at=NOON - timedelta(hours=1))
    st = state_after(grid, [(old, NOON - timedelta(hours=6)), (bad, NOON - timedelta(hours=5)),
                            (w1, NOON - timedelta(hours=4)), (w2, NOON - timedelta(hours=3))])
    unit = unit_at(grid, [], NOON, state=st)
    assert unit.rung == 4 and ids(unit) == ["w1"]


def test_rung_4_prefers_never_aired_stock() -> None:
    grid = doc_grid()     # con interrupciones: los jingles son emitibles
    j = seg("j", "jingle", 8.0)
    w = seg("w1", "weather", 30.0, status="retired")
    st = state_after(grid, [(w, NOON - timedelta(hours=2))])
    unit = unit_at(grid, [j], NOON, state=st)
    assert unit.rung == 4 and ids(unit) == ["j"]


def test_rung_4_never_breaks_budget_or_fiction_rule() -> None:
    grid = simple_grid(["music"], {"weather": 1, "consultorio": 1})
    heavy = seg("heavy", "weather", 790.0, status="retired")
    st = state_after(grid, [(heavy, NOON - timedelta(minutes=14))])
    assert unit_at(grid, [], NOON, state=st).rung == 5
    cons = seg("c", "consultorio", 30.0, status="retired")
    w = seg("w", "weather", 30.0, status="retired")
    st2 = state_after(grid, [(cons, NOON - timedelta(hours=2)), (w, NOON - timedelta(minutes=1))])
    unit = unit_at(grid, [], NOON, state=st2)
    assert ids(unit) != ["c"]


def test_rung_5_emergency() -> None:
    unit = unit_at(simple_grid(["music"]), [seg("w", "weather", 30.0)], NOON)
    assert unit.rung == 5 and unit.segments == () and unit.pattern_key is None


def test_empty_stock_view_default() -> None:
    unit = next_unit(SchedulerState(grid=GridConfig()), StockView(), NOON, "default",
                     random.Random(0))
    assert unit.is_emergency


# ── advance_state ─────────────────────────────────────────────────────────────

def test_advance_state_appends_history_and_trims() -> None:
    grid = simple_grid(["music"])
    m, i = seg("m", "music", 200.0), seg("i", "host_intro", 20.0, parent_id="m")
    unit = PlayUnit((i, m), "x", 1, pattern_key="default/todo")
    st = advance_state(SchedulerState(grid=grid), unit, NOON)
    assert [(e.segment_id, e.started_at) for e in st.history] == [
        ("i", NOON), ("m", NOON + timedelta(seconds=20))]
    assert st.history[1].ended_at == NOON + timedelta(seconds=220)
    assert set(st.segments) == {"i", "m"} and st.pattern_pos == {"default/todo": 1}
    # Una interrupción no consume hueco
    st2 = advance_state(st, PlayUnit((seg("t", "time_signal", 3.0),), "x", 1), NOON)
    assert st2.pattern_pos == {"default/todo": 1}
    # Lo que queda fuera del horizonte se descarta
    far = NOON + history_horizon(grid) + timedelta(hours=1)
    st3 = advance_state(st2, PlayUnit((seg("z", "music", 10.0),), "x", 1), far)
    assert [e.segment_id for e in st3.history] == ["z"] and set(st3.segments) == {"z"}


def test_history_horizon_covers_cooldowns() -> None:
    assert history_horizon(doc_grid()) == timedelta(minutes=720)
    assert history_horizon(simple_grid(["music"])) == timedelta(hours=3)
