"""
Scheduler de parrilla (§4.3): decide la próxima ``PlayUnit``.

API
---
``next_unit(state, stock, now, mode, rng) -> PlayUnit`` es una función casi pura
(invariante §1.7): sin BD, sin reloj, sin I/O. Todo lo que sabe llega en
``SchedulerState`` (configuración + historial + cursor del patrón) y en el
``StockView``. El único azar sale de ``rng``. **Nunca devuelve None**: en el peor
caso devuelve una unidad vacía de peldaño 5 (emergencia) y quien llama emite
``assets/emergency/``.

Tras emitir una unidad, quien llama actualiza el estado con
``advance_state(state, unit, started_at)`` (añade las emisiones al historial y avanza
el cursor del patrón). Una emisora respaldada por SQLite puede, en su lugar,
reconstruir ``history``/``segments`` desde ``play_log`` en cada paso y conservar solo
``pattern_pos`` en memoria (así lo hace ``core/playout.py``). En un bucle puro (tests,
simulaciones sin BD) quien llama debe además retirar del stock la palabra emitida,
igual que hace el playout en la BD (``retired``).

Algoritmo (en el orden de §4.3)
-------------------------------
1. **Interrupciones.** Para cada regla ``interrupts`` del modo: si su último disparo
   (``when``, hora local) fue hace <= ``max_late_seconds`` y desde entonces no ha
   sonado ese kind, se emite un segmento de ese kind. Si el segmento lleva etiqueta
   ``hour:YYYY-MM-DDTHH`` tiene que ser la de la hora del disparo (señal horaria).
   Después, segmentos ``priority > 0`` de kinds del modo sin regla propia (urgentes;
   respetan presupuesto y separación factual/ficción). Ambas salen con
   ``interrupt=True``: la emisora puede cortar lo que suene para darles paso.
2. **Franja y patrón.** La franja (``daypart``) activa según ``now`` (hora local de
   pared) y ``mode`` da un ``pattern`` cíclico de huecos: ``music``, ``talk`` o
   ``jingle``. El cursor vive en ``state.pattern_pos[f"{modo}/{franja}"]``. Un hueco
   ``talk`` con ``talk_pool`` vacío es un hueco ``music``.
3. **Presupuesto de charla** (``budget.py``): si la ventana ya está en el tope, o
   ningún candidato de palabra cabe, el hueco ``talk`` se convierte en ``music``.
4. **Filtros**: ``ready`` y sin caducar (``StockView`` + ``is_live``), cooldown por
   kind (``cooldowns_minutes``), no repetido (palabra: no emitida en el historial;
   música: no entre las últimas ``MUSIC_REPEAT_PLAYS`` canciones —acotado a la mitad
   del catálogo— ni del mismo artista que la última canción; jingle: no el mismo que
   el último), y **nunca ficción justo después de factual** (invariante §1.4: hace
   falta música o jingle entre medias; se mira ``Segment.factual`` del último
   segmento emitido, y si no se conoce se supone factual).
5. **Elección**: palabra → kind por pesos de ``talk_pool`` con ``rng`` y, dentro del
   kind, el que caduca antes (luego el más antiguo); música/jingle → uniforme con
   ``rng`` entre los candidatos (ordenados de forma estable).
6. **Vinculación**: si sale música con un ``host_intro`` hijo (``parent_id``) emitible,
   la unidad es ``[host_intro, music]`` (si la intro cabe en el presupuesto, no está
   en cooldown y respeta §1.4; si no, la música va sola).
7. **Escalera de degradación** (§8), ``PlayUnit.rung``:
   1. selección ideal según la parrilla;
   2. el mismo hueco relajando cooldowns y repetición;
   3. cualquier música ``ready`` (se sigue prefiriendo otro artista);
   4. repetir algo: stock emitible del modo o segmentos ya emitidos (``retired``) del
      historial, nunca emitidos primero y luego el emitido hace más tiempo;
   5. emergencia: ``segments == ()``.
   El presupuesto de charla y §1.4 no se relajan nunca: antes que romperlos se baja
   al peldaño 5, que no es silencio.

Jingles
-------
Entran por dos vías: huecos ``jingle`` en el ``pattern`` (identificativo de emisora,
separan factual de ficción) y como **relleno** antes de una interrupción: si no queda
ninguna canción que termine a tiempo para la próxima señal horaria, se rellena con
jingles/stingers cortos (solo en modos con interrupciones; no consumen hueco del
patrón). No cuentan como palabra.

Preferencia horaria
-------------------
Si la próxima interrupción del modo tendrá segmento disponible, se prefieren unidades
que acaben antes de ``disparo + max_late_seconds``; en música, además, las que no
dejen un hueco menor que la canción más corta (para no quedarse sin nada que quepa).
Es una preferencia: la puntualidad estricta la da la emisora interrumpiendo (ver
``next_interrupt_at`` en ``rules.py``).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from radio.core.config import GridConfig, InterruptRule, ModeConfig
from radio.core.models import PlayLogEntry, Segment, StockView
from radio.grid.budget import entry_interval, fits_budget, is_talk, talk_ratio
from radio.grid.rules import (
    HOUR_TAG_PREFIX,
    aware,
    daypart_for,
    fires_per_window,
    hour_tag,
    last_fire_at,
    next_fire_at,
    resolve_mode,
    zone,
)

# ── Constantes ────────────────────────────────────────────────────────────────

# Peldaños de la escalera de degradación (§8)
RUNG_IDEAL = 1
RUNG_RELAXED = 2
RUNG_ANY_MUSIC = 3
RUNG_REPEAT = 4
RUNG_EMERGENCY = 5

# Kinds que solo se emiten pegados a su "padre" (paso 6), nunca sueltos
LINKED_KINDS: frozenset[str] = frozenset({"host_intro"})

# Kinds que separan factual de ficción (§1.4)
SEPARATOR_KINDS: frozenset[str] = frozenset({"music", "jingle", "stinger", "emergency"})

# Kinds cortos para rellenar hasta una interrupción
FILLER_KINDS: tuple[str, ...] = ("jingle", "stinger")

# Una canción no se repite dentro de las últimas N canciones (acotado al catálogo)
MUSIC_REPEAT_PLAYS = 30

# Historial mínimo que conviene pasar en SchedulerState
MIN_HISTORY_HORIZON = timedelta(hours=3)

# Duración supuesta de un segmento de interrupción sin stock (para la reserva)
DEFAULT_INTERRUPT_S = 10.0

ARTIST_PREFIX = "artist:"
_REUSABLE_STATUSES = frozenset({"ready", "retired"})


# ── Modelos ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayUnit:
    """
    Segmentos que se emiten juntos, en orden (p. ej. ``[host_intro, music]``).

    - ``rung``: peldaño de la escalera de degradación (1–5). Con 5 ``segments`` está
      vacío y se emite el bucle de emergencia.
    - ``interrupt``: la unidad viene del paso 1 (interrupción o prioridad > 0); la
      emisora puede cortar el audio en curso para emitirla.
    - ``slot``: hueco del patrón que cubre (``music``/``talk``/``jingle``) o None.
    - ``pattern_key``: cursor que consume (``"modo/franja"``) o None si no consume
      (interrupciones, rellenos, emergencia).
    """
    segments: tuple[Segment, ...]
    reason: str
    rung: int
    interrupt: bool = False
    slot: str | None = None
    pattern_key: str | None = None
    daypart: str | None = None
    mode: str = "default"

    @property
    def duration_s(self) -> float:
        return sum(s.duration_s for s in self.segments)

    @property
    def is_emergency(self) -> bool:
        return not self.segments


@dataclass(frozen=True)
class SchedulerState:
    """
    Todo lo que el scheduler sabe además del stock.

    - ``grid``: la parrilla (grid.yaml).
    - ``history``: emisiones recientes, la más antigua primero (``DB.list_play_log``);
      conviene cubrir al menos ``history_horizon(grid)``.
    - ``pattern_pos``: cursor de cada patrón, por ``"modo/franja"``.
    - ``segments``: segmentos referenciados por ``history`` (por id), con su estado
      actual. Sirven para saber si lo último fue factual, el artista de la última
      canción y qué se puede repetir en el peldaño 4. Si falta alguno, se asume lo
      prudente (factual, sin artista, no repetible).
    """
    grid: GridConfig
    history: tuple[PlayLogEntry, ...] = ()
    pattern_pos: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    segments: Mapping[str, Segment] = field(default_factory=lambda: MappingProxyType({}))


def history_horizon(grid: GridConfig) -> timedelta:
    """Historial necesario: ventana de charla, el cooldown más largo y 3 h como mínimo."""
    longest_cd = max(grid.cooldowns_minutes.values(), default=0.0)
    return max(
        MIN_HISTORY_HORIZON,
        timedelta(minutes=grid.talk_budget.window_minutes),
        timedelta(minutes=longest_cd),
    )


def advance_state(state: SchedulerState, unit: PlayUnit, started_at: datetime) -> SchedulerState:
    """
    Estado tras emitir ``unit`` desde ``started_at`` (segmentos seguidos, duración
    nominal). Añade las emisiones al historial (ids negativos, sintéticos), recorta
    lo que queda fuera de ``history_horizon`` y avanza el cursor del patrón.
    """
    entries = list(state.history)
    next_id = min([0, *(e.id for e in entries)]) - 1
    segments = dict(state.segments)
    t = started_at
    for seg in unit.segments:
        end = t + timedelta(seconds=seg.duration_s)
        entries.append(PlayLogEntry(
            id=next_id, segment_id=seg.id, kind=seg.kind, mode=unit.mode,
            started_at=t, ended_at=end, skipped=False, duration_s=seg.duration_s,
        ))
        segments[seg.id] = seg
        next_id -= 1
        t = end
    cutoff = t - history_horizon(state.grid)
    kept = tuple(e for e in entries if e.started_at >= cutoff)
    referenced = {e.segment_id for e in kept}
    return SchedulerState(
        grid=state.grid,
        history=kept,
        pattern_pos=advance_pattern(state.pattern_pos, unit),
        segments=MappingProxyType({k: v for k, v in segments.items() if k in referenced}),
    )


def advance_pattern(pattern_pos: Mapping[str, int], unit: PlayUnit) -> Mapping[str, int]:
    """Cursor del patrón tras emitir ``unit`` (solo avanza si consumió un hueco)."""
    pos = dict(pattern_pos)
    if unit.pattern_key is not None:
        pos[unit.pattern_key] = pos.get(unit.pattern_key, 0) + 1
    return MappingProxyType(pos)


# ── Contexto de una decisión ──────────────────────────────────────────────────

@dataclass(frozen=True)
class _Deadline:
    """Próxima interrupción con segmento disponible."""
    fire_at: datetime
    late_s: float


class _Ctx:
    """Datos derivados que se reutilizan durante una llamada a ``next_unit``."""

    def __init__(
        self, state: SchedulerState, stock: StockView, now: datetime, mode: str,
        rng: random.Random,
    ) -> None:
        self.state = state
        self.grid = state.grid
        self.tz: ZoneInfo = zone(state.grid)
        # En UTC: la aritmética con fechas de la misma zona ignora ``fold`` (cambio de hora)
        self.now = aware(now, self.tz).astimezone(UTC)
        self.mode, self.cfg = resolve_mode(state.grid, mode)
        self.stock = stock
        self.rng = rng
        self.history = state.history
        self.window = timedelta(minutes=state.grid.talk_budget.window_minutes)
        self.max_ratio = state.grid.talk_budget.max_ratio
        self.rule_kinds = frozenset(r.kind for r in self.cfg.interrupts)
        self.all_rule_kinds = frozenset(
            r.kind for m in state.grid.modes.values() for r in m.interrupts
        )
        self.allowed = _allowed_kinds(self.cfg)
        self._live: dict[str, list[Segment]] = {}
        self._children: dict[str, dict[str, list[Segment]]] = {}
        # Solo la palabra que puede caer en alguna ventana proyectada (termina tras now - ventana)
        self.intervals = tuple(
            iv for iv in map(entry_interval, self.history)
            if iv.talk and iv.end > self.now - self.window
        )
        self.aired_ids = frozenset(e.segment_id for e in self.history if e.segment_id)
        self.reserve_s = self._reserve()
        self.deadline = self._deadline()

    # ── Stock ────────────────────────────────────────────────────────────────

    def live(self, kind: str) -> list[Segment]:
        """Emitibles de ``kind`` en ``now``, en orden estable (created_at, id)."""
        if kind not in self._live:
            segs = [s for s in self.stock.get(kind) if s.is_live(self.now)]
            self._live[kind] = sorted(segs, key=lambda s: (s.created_at, s.id))
        return list(self._live[kind])

    def intros_for(self, parent_id: str, kind: str) -> list[Segment]:
        """Segmentos vinculados de ``kind`` cuyo padre es ``parent_id``."""
        if kind not in self._children:
            children: dict[str, list[Segment]] = {}
            for seg in self.live(kind):
                if seg.parent_id is not None:
                    children.setdefault(seg.parent_id, []).append(seg)
            self._children[kind] = children
        return self._children[kind].get(parent_id, [])

    # ── Historial ────────────────────────────────────────────────────────────

    def in_cooldown(self, kind: str) -> bool:
        minutes = self.grid.cooldowns_minutes.get(kind, 0.0)
        if minutes <= 0:
            return False
        limit = self.now - timedelta(minutes=minutes)
        return any(e.kind == kind and e.started_at > limit for e in self.history)

    def last_factual(self) -> bool:
        """¿Lo último emitido fue palabra factual (o desconocida)? §1.4."""
        if not self.history:
            return False
        last = self.history[-1]
        if last.kind in SEPARATOR_KINDS:
            return False
        seg = self.state.segments.get(last.segment_id or "")
        return True if seg is None else seg.factual

    def last_music_artists(self) -> frozenset[str]:
        for e in reversed(self.history):
            if e.kind == "music":
                seg = self.state.segments.get(e.segment_id or "")
                return _artists(seg) if seg else frozenset()
        return frozenset()

    def recent_music_ids(self, n: int) -> frozenset[str]:
        ids = [e.segment_id for e in self.history if e.kind == "music" and e.segment_id]
        return frozenset(ids[-n:]) if n > 0 else frozenset()

    def last_aired_at(self, seg_id: str) -> datetime | None:
        for e in reversed(self.history):
            if e.segment_id == seg_id:
                return e.started_at
        return None

    # ── Reglas duras ─────────────────────────────────────────────────────────

    def sequence_ok(self, segs: Sequence[Segment]) -> bool:
        """§1.4 a lo largo de la secuencia, empezando por lo último emitido."""
        prev_factual = self.last_factual()
        for seg in segs:
            if prev_factual and not seg.factual and seg.kind not in SEPARATOR_KINDS:
                return False
            prev_factual = seg.factual and seg.kind not in SEPARATOR_KINDS
        return True

    def budget_ok(self, segs: Sequence[Segment]) -> bool:
        return fits_budget(
            self.intervals, [(s.kind, s.duration_s) for s in segs], self.now,
            window=self.window, max_ratio=self.max_ratio, reserve_s=self.reserve_s,
        )

    def hard_ok(self, segs: Sequence[Segment]) -> bool:
        return self.sequence_ok(segs) and self.budget_ok(segs)

    # ── Interrupciones ───────────────────────────────────────────────────────

    def interrupt_candidates(self, kind: str, fire_at: datetime, at: datetime) -> list[Segment]:
        """Segmentos de ``kind`` emitibles en ``at`` para el disparo de ``fire_at``."""
        tag = hour_tag(fire_at, self.tz)
        out = []
        for seg in self.stock.get(kind):
            if not seg.is_live(at) or seg.id in self.aired_ids:
                continue
            hour_tags = [t for t in seg.tags if t.startswith(HOUR_TAG_PREFIX)]
            if hour_tags and tag not in hour_tags:
                continue
            out.append(seg)
        return sorted(out, key=lambda s: (-s.priority, s.created_at, s.id))

    def _reserve(self) -> float:
        total = 0.0
        for rule in self.cfg.interrupts:
            durations = [s.duration_s for s in self.stock.get(rule.kind)]
            longest = max(durations, default=DEFAULT_INTERRUPT_S)
            if is_talk(rule.kind):
                total += fires_per_window(rule, self.grid.talk_budget.window_minutes) * longest
        return total

    def _deadline(self) -> _Deadline | None:
        best: _Deadline | None = None
        for rule in self.cfg.interrupts:
            fire = next_fire_at(rule, self.now, self.tz)
            if fire is None:
                continue
            if not self.interrupt_candidates(rule.kind, fire, fire):
                continue
            if best is None or fire < best.fire_at:
                best = _Deadline(fire, rule.max_late_seconds)
        return best

    def fits_deadline(self, duration_s: float, *, min_gap_s: float | None = None) -> bool:
        """
        ¿Termina a tiempo para la próxima interrupción? Con ``min_gap_s`` exige además
        acabar ya dentro de la tolerancia o dejar al menos ese hueco libre.
        """
        if self.deadline is None:
            return True
        remaining = (self.deadline.fire_at - self.now).total_seconds()
        if duration_s > remaining + self.deadline.late_s:
            return False
        if min_gap_s is None:
            return True
        return duration_s >= remaining or remaining - duration_s >= min_gap_s


def _artists(seg: Segment) -> frozenset[str]:
    return frozenset(t for t in seg.tags if t.startswith(ARTIST_PREFIX))


def _allowed_kinds(cfg: ModeConfig) -> frozenset[str]:
    """Kinds que puede emitir un modo (lo que no esté aquí no suena en ese modo)."""
    kinds = {"music", *LINKED_KINDS}
    for part in cfg.dayparts:
        kinds.update(k for k, w in part.talk_pool.items() if w > 0)
        if "jingle" in part.pattern:
            kinds.add("jingle")
    if cfg.interrupts:
        kinds.update(FILLER_KINDS)
        kinds.update(r.kind for r in cfg.interrupts)
    return frozenset(kinds)


def _weighted_choice(items: Sequence[str], weights: Sequence[float], rng: random.Random) -> str:
    """Elección ponderada con un único ``rng.random()``."""
    r = rng.random() * sum(weights)
    acc = 0.0
    for item, w in zip(items, weights, strict=True):
        acc += w
        if r < acc:
            return item
    return items[-1]


def _uniform(options: Sequence[tuple[Segment, ...]], rng: random.Random) -> tuple[Segment, ...]:
    return options[min(int(rng.random() * len(options)), len(options) - 1)]


# ── Paso 1: interrupciones ────────────────────────────────────────────────────

def _interrupt_unit(ctx: _Ctx) -> PlayUnit | None:
    for rule in ctx.cfg.interrupts:
        unit = _rule_unit(ctx, rule)
        if unit is not None:
            return unit
    urgent = [
        seg
        for kind in sorted(ctx.allowed - ctx.rule_kinds - LINKED_KINDS)
        for seg in ctx.live(kind)
        if seg.priority > 0 and seg.id not in ctx.aired_ids and ctx.hard_ok([seg])
    ]
    if urgent:
        seg = min(urgent, key=lambda s: (-s.priority, s.created_at, s.id))
        return PlayUnit(
            (seg,), f"prioridad {seg.priority}: {seg.kind} urgente", RUNG_IDEAL,
            interrupt=True, slot=None, mode=ctx.mode,
        )
    return None


def _rule_unit(ctx: _Ctx, rule: InterruptRule) -> PlayUnit | None:
    fire = last_fire_at(rule, ctx.now, ctx.tz)
    if fire is None:
        return None
    late = (ctx.now - fire).total_seconds()
    if late > rule.max_late_seconds:
        return None
    if any(e.kind == rule.kind and e.started_at >= fire for e in ctx.history):
        return None
    cands = ctx.interrupt_candidates(rule.kind, fire, ctx.now)
    if not cands:
        return None
    local = fire.astimezone(ctx.tz)
    return PlayUnit(
        (cands[0],),
        f"interrupción {rule.kind} ({rule.when}) de las {local:%H:%M}, {late:.0f}s tarde",
        RUNG_IDEAL, interrupt=True, mode=ctx.mode,
    )


# ── Pasos 3–6: selección por hueco ────────────────────────────────────────────

@dataclass(frozen=True)
class _Pick:
    segments: tuple[Segment, ...]
    note: str
    filler: bool = False


def _link(ctx: _Ctx, music: Segment, relax: bool) -> tuple[Segment, ...]:
    """Paso 6: ``[host_intro, music]`` si hay intro hija emitible y válida."""
    for kind in sorted(LINKED_KINDS):
        if not relax and ctx.in_cooldown(kind):
            continue
        for intro in ctx.intros_for(music.id, kind):
            if intro.id in ctx.aired_ids:
                continue
            unit = (intro, music)
            if ctx.hard_ok(unit):
                return unit
    return (music,)


def _pick_music(ctx: _Ctx, rung: int) -> _Pick | None:
    """Música para los peldaños 1–3 (con vinculación y preferencia horaria)."""
    music = ctx.live("music")
    if not music:
        return None
    last_artists = ctx.last_music_artists()
    other_artist = [s for s in music if not (_artists(s) & last_artists)]
    note = ""
    if rung == RUNG_IDEAL:
        if ctx.in_cooldown("music"):
            return None
        recent = ctx.recent_music_ids(min(MUSIC_REPEAT_PLAYS, len(music) // 2))
        cands = [s for s in other_artist if s.id not in recent]
    elif rung == RUNG_RELAXED:
        cands = other_artist
    else:
        last_id = next(
            (e.segment_id for e in reversed(ctx.history) if e.kind == "music"), None
        )
        cands = other_artist or [s for s in music if s.id != last_id] or music
        if not other_artist:
            note = "; mismo artista (no hay otro)"
    if not cands:
        return None
    options = [_link(ctx, s, relax=rung > RUNG_IDEAL) for s in cands]
    options = [o for o in options if ctx.hard_ok(o)]
    if not options:
        return None
    if ctx.deadline is not None:
        min_gap = min(s.duration_s for s in music)
        tier1 = [o for o in options if ctx.fits_deadline(_dur(o), min_gap_s=min_gap)]
        tier2 = [o for o in options if ctx.fits_deadline(_dur(o))]
        if tier1:
            options = tier1
        elif tier2:
            options = tier2
        else:
            filler = _pick_filler(ctx)
            if filler is not None:
                return filler
            note += "; ninguna acaba antes de la interrupción"
    chosen = _uniform(options, ctx.rng)
    linked = " con host_intro" if len(chosen) > 1 else ""
    return _Pick(chosen, f"música{linked} ({len(options)} candidatas){note}")


def _dur(segs: Sequence[Segment]) -> float:
    return sum(s.duration_s for s in segs)


def _pick_filler(ctx: _Ctx) -> _Pick | None:
    """Relleno corto hasta la interrupción (solo modos con interrupciones)."""
    if not ctx.cfg.interrupts:
        return None
    for kind in FILLER_KINDS:
        cands = [(s,) for s in ctx.live(kind) if ctx.fits_deadline(s.duration_s)]
        if cands:
            return _Pick(_uniform(cands, ctx.rng), f"{kind} de relleno hasta la interrupción",
                         filler=True)
    return None


def _pick_jingle(ctx: _Ctx, rung: int) -> _Pick | None:
    jingles = ctx.live("jingle")
    if rung == RUNG_IDEAL:
        if ctx.in_cooldown("jingle"):
            return None
        last = next((e.segment_id for e in reversed(ctx.history) if e.kind == "jingle"), None)
        if len(jingles) > 1:
            jingles = [s for s in jingles if s.id != last]
    options = [(s,) for s in jingles]
    if not options:
        return None
    fitting = [o for o in options if ctx.fits_deadline(_dur(o))]
    chosen = _uniform(fitting or options, ctx.rng)
    return _Pick(chosen, f"jingle ({len(options)} candidatos)")


class _BudgetExhausted(Exception):
    """Hay palabra candidata pero ninguna cabe en el presupuesto (paso 3)."""


def _pick_talk(ctx: _Ctx, pool: Mapping[str, float], rung: int) -> _Pick | None:
    kinds: list[str] = []
    weights: list[float] = []
    by_kind: dict[str, list[Segment]] = {}
    blocked_by_budget = False
    for kind in sorted(pool):
        weight = pool[kind]
        if weight <= 0 or kind in LINKED_KINDS or kind in ctx.rule_kinds:
            continue
        if rung == RUNG_IDEAL and ctx.in_cooldown(kind):
            continue
        cands = [s for s in ctx.live(kind) if ctx.sequence_ok([s])]
        if rung == RUNG_IDEAL:
            cands = [s for s in cands if s.id not in ctx.aired_ids]
        fitting = [s for s in cands if ctx.budget_ok([s])]
        if cands and not fitting:
            blocked_by_budget = True
        if fitting:
            kinds.append(kind)
            weights.append(weight)
            by_kind[kind] = fitting
    if not kinds:
        if blocked_by_budget:
            raise _BudgetExhausted
        return None
    kind = _weighted_choice(kinds, weights, ctx.rng)
    cands = by_kind[kind]
    on_time = [s for s in cands if ctx.fits_deadline(s.duration_s)]
    far = datetime.max.replace(tzinfo=UTC)
    seg = min(on_time or cands, key=lambda s: (s.expires_at or far, s.created_at, s.id))
    return _Pick((seg,), f"palabra {kind} (pool {dict(zip(kinds, weights, strict=True))})")


# ── Peldaño 4: repetir ────────────────────────────────────────────────────────

def _pick_repeat(ctx: _Ctx) -> _Pick | None:
    """Stock emitible del modo o ya emitido (``retired``); nunca emitido primero."""
    excluded = ctx.all_rule_kinds | LINKED_KINDS
    pool: dict[str, Segment] = {}
    for kind in sorted(ctx.allowed - excluded):
        for seg in ctx.live(kind):
            pool[seg.id] = seg
    for seg in ctx.state.segments.values():
        if (
            seg.kind in ctx.allowed and seg.kind not in excluded
            and seg.status in _REUSABLE_STATUSES
            and (seg.expires_at is None or seg.expires_at > ctx.now)
        ):
            pool.setdefault(seg.id, seg)
    epoch = datetime.min.replace(tzinfo=UTC)

    def order(seg: Segment) -> tuple[int, datetime, datetime, str]:
        last = ctx.last_aired_at(seg.id)
        return (0 if last is None else 1, last or epoch, seg.created_at, seg.id)

    for seg in sorted(
        (s for s in pool.values() if not any(t.startswith(HOUR_TAG_PREFIX) for t in s.tags)),
        key=order,
    ):
        if ctx.hard_ok([seg]):
            return _Pick((seg,), f"repetición de {seg.kind} (el más antiguo primero)")
    return None


# ── API ───────────────────────────────────────────────────────────────────────

def next_unit(
    state: SchedulerState, stock: StockView, now: datetime, mode: str, rng: random.Random
) -> PlayUnit:
    """Próxima unidad a emitir (ver docstring del módulo). Nunca devuelve None."""
    ctx = _Ctx(state, stock, now, mode, rng)

    # 1. Interrupciones
    unit = _interrupt_unit(ctx)
    if unit is not None:
        return unit

    # 2. Franja y hueco del patrón
    part = daypart_for(ctx.grid, ctx.now, ctx.mode)
    key = f"{ctx.mode}/{part.name}"
    pos = state.pattern_pos.get(key, 0)
    slot = part.pattern[pos % len(part.pattern)]
    pool = {k: w for k, w in part.talk_pool.items() if w > 0}
    effective = slot
    why = f"{part.name}[{pos % len(part.pattern)}]={slot}"
    if slot == "talk" and not pool:
        effective = "music"
        why += "→music (talk_pool vacío)"

    # 3. Presupuesto de charla
    if effective == "talk" and talk_ratio(ctx.history, ctx.now, ctx.window) >= ctx.max_ratio:
        effective = "music"
        why += "→music (presupuesto de charla agotado)"

    def build(pick: _Pick, rung: int) -> PlayUnit:
        return PlayUnit(
            pick.segments, f"{why}: {pick.note}", rung,
            slot=None if pick.filler else slot,
            pattern_key=None if pick.filler else key,
            daypart=part.name, mode=ctx.mode,
        )

    # 4–6. Peldaños 1 y 2 sobre el hueco
    pickers: dict[str, Callable[[int], _Pick | None]] = {
        "music": lambda r: _pick_music(ctx, r),
        "jingle": lambda r: _pick_jingle(ctx, r),
        "talk": lambda r: _pick_talk(ctx, pool, r),
    }
    for rung in (RUNG_IDEAL, RUNG_RELAXED):
        try:
            pick = pickers[effective](rung)
        except _BudgetExhausted:
            effective = "music"
            why += "→music (ninguna palabra cabe en el presupuesto)"
            pick = _pick_music(ctx, rung)
        if pick is not None:
            return build(pick, rung)

    # 7. Escalera de degradación
    why += f" sin candidatos ({effective})"
    pick = _pick_music(ctx, RUNG_ANY_MUSIC)
    if pick is not None:
        return build(pick, RUNG_ANY_MUSIC)
    pick = _pick_repeat(ctx)
    if pick is not None:
        return build(pick, RUNG_REPEAT)
    return PlayUnit((), f"{why}: nada emitible → bucle de emergencia", RUNG_EMERGENCY,
                    daypart=part.name, mode=ctx.mode)
