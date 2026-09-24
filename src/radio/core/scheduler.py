"""
Scheduler de parrilla de Radio Parra: decide qué tipo de segmento suena a continuación.

Lógica pura: sin I/O, sin base de datos. Recibe el historial de lo emitido y el
número de segmentos listos por tipo, y devuelve un ``SegmentKind`` (o ``None``).

Convenciones
------------
- ``TALK_KINDS`` = {host_intro, factual, fiction, time_signal}: cuentan como "palabra".
- ``jingle`` no es música ni palabra a efectos del presupuesto: su duración cuenta en
  el denominador (tiempo de antena) pero no en el numerador. Además es "transparente"
  para el cálculo de la racha de palabra (no la corta ni la alarga), para que un
  jingle no sirva de truco para encadenar palabra indefinidamente.
- Las fechas deben ser *aware*. Si llega una fecha naive (p. ej. de ``SystemClock``) se
  interpreta como hora local en ``grid.timezone``.

Reglas de ``next_kind`` (en orden de evaluación)
------------------------------------------------
1. Nunca se devuelve un tipo con ``available.get(kind, 0) == 0``.
2. Señal horaria: si ``grid.time_signal_enabled``, hay ``time_signal`` disponible, el
   minuto local es < 5 y no ha sonado otra en los últimos ``cooldowns.time_signal``
   minutos → ``"time_signal"``. Máxima prioridad: ignora racha y presupuesto (dura
   pocos segundos).
3. Cooldowns por tipo (factual, fiction, jingle, time_signal): un tipo queda bloqueado
   si empezó un registro de ese tipo hace menos de N minutos.
4. Racha de palabra: la suma de ``duration_s`` de los registros de palabra consecutivos
   al final del historial (saltando jingles) debe ser < ``slot.max_talk_run_min``; si ya
   alcanza el límite, la palabra queda bloqueada.
5. Presupuesto de palabra: en la ventana móvil de 60 min (duraciones recortadas a la
   ventana), palabra / total debe mantenerse ≤ ``min(grid.talk_budget_ratio,
   1 - slot.music_ratio)``. Si ya está en el límite o por encima, la palabra queda
   bloqueada. Con historial vacío se permite.
6. Elección:
   a. Jingle de transición: si lo último (no jingle) fue música, el jingle está
      disponible y fuera de cooldown, con probabilidad ``JINGLE_PROBABILITY``.
   b. Si hay tipos de palabra permitidos (host_intro, factual, fiction; la señal
      horaria solo entra por la regla 2), se elige uno por pesos
      factual 3 > host_intro 2 > fiction 1. host_intro se favorece (×3) justo después
      de música si no ha habido palabra en los últimos ``HOST_INTRO_QUIET_MIN``
      minutos. host_intro nunca se repite dos veces seguidas.
7. Por defecto: ``"music"`` si hay; si no, cualquier tipo de palabra permitido; si no,
   cualquier tipo disponible fuera de cooldown; si no, cualquier tipo disponible
   ignorando todas las restricciones blandas (mejor romper el presupuesto que dejar
   silencio); si no hay nada, ``None``.
8. Determinista: el único azar proviene de ``self.rng`` y los candidatos se recorren
   siempre en un orden fijo.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from radio.core.config import GridConfig, TimeSlot
from radio.core.models import SegmentKind

# ── Constantes ────────────────────────────────────────────────────────────────

# Tipos que consumen presupuesto de palabra
TALK_KINDS: frozenset[SegmentKind] = frozenset({"host_intro", "factual", "fiction", "time_signal"})

# Orden fijo de todos los tipos (para recorridos deterministas)
ALL_KINDS: tuple[SegmentKind, ...] = (
    "music", "factual", "host_intro", "fiction", "jingle", "time_signal",
)

# Pesos base para elegir entre tipos de palabra (regla 6b)
TALK_WEIGHTS: dict[SegmentKind, float] = {"factual": 3.0, "host_intro": 2.0, "fiction": 1.0}

# Multiplicador del peso de host_intro tras música sin palabra reciente
HOST_INTRO_BOOST = 3.0

# Minutos sin palabra para considerar que host_intro "abre" un bloque
HOST_INTRO_QUIET_MIN = 15

# Probabilidad de meter un jingle de transición tras música
JINGLE_PROBABILITY = 0.2

# Ventana móvil del presupuesto de palabra
BUDGET_WINDOW = timedelta(minutes=60)

# La señal horaria puede sonar durante los primeros N minutos de la hora
TIME_SIGNAL_WINDOW_MIN = 5

DEFAULT_SLOT = TimeSlot(name="default", start="00:00", end="00:00")


# ── Modelos ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayRecord:
    """Registro de un segmento ya emitido (o en emisión)."""
    kind: SegmentKind
    started_at: datetime   # aware
    duration_s: float


# ── Utilidades ────────────────────────────────────────────────────────────────

def _parse_hhmm(value: str) -> int:
    """Convierte "HH:MM" en minutos desde medianoche."""
    hours, minutes = value.strip().split(":")
    return int(hours) * 60 + int(minutes)


def _slot_contains(slot: TimeSlot, minute_of_day: int) -> bool:
    """Indica si el minuto del día cae en la franja (soporta cruce de medianoche)."""
    start = _parse_hhmm(slot.start)
    end = _parse_hhmm(slot.end)
    if start == end:           # franja de 24 h
        return True
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """Decide el siguiente tipo de segmento según la parrilla (ver docstring del módulo)."""

    def __init__(self, grid: GridConfig, rng: random.Random | None = None) -> None:
        self.grid = grid
        self.rng = rng if rng is not None else random.Random()
        self._tz = ZoneInfo(grid.timezone)
        self._last_reason = "sin decisiones todavía"

    # ── API pública ──────────────────────────────────────────────────────────

    def slot_for(self, t: datetime) -> TimeSlot:
        """Devuelve la franja activa en el instante t (primera que encaje)."""
        local = self._to_local(t)
        minute_of_day = local.hour * 60 + local.minute
        for slot in self.grid.slots:
            if _slot_contains(slot, minute_of_day):
                return slot
        return DEFAULT_SLOT

    def next_kind(
        self,
        now: datetime,
        history: Sequence[PlayRecord],
        available: Mapping[str, int],
    ) -> SegmentKind | None:
        """Elige el siguiente tipo de segmento. ``None`` si no hay nada disponible."""
        now = self._aware(now)
        local = self._to_local(now)
        slot = self.slot_for(now)

        def has(kind: SegmentKind) -> bool:
            return available.get(kind, 0) > 0

        # Regla 1: sin nada disponible → None
        if not any(has(k) for k in ALL_KINDS):
            return self._decide(None, "nada disponible")

        # Regla 2: señal horaria
        if (
            self.grid.time_signal_enabled
            and has("time_signal")
            and local.minute < TIME_SIGNAL_WINDOW_MIN
            and not self._in_cooldown("time_signal", now, history)
        ):
            return self._decide("time_signal", f"señal horaria {local:%H:%M} ({slot.name})")

        # Reglas 4 y 5: restricciones blandas de palabra
        run_s = self._talk_run_seconds(history)
        run_limit_s = slot.max_talk_run_min * 60.0
        budget = min(self.grid.talk_budget_ratio, 1.0 - slot.music_ratio)
        ratio = self._talk_ratio(now, history)
        talk_blocked_reason: str | None = None
        if run_s >= run_limit_s:
            talk_blocked_reason = f"racha de palabra {run_s:.0f}s ≥ {run_limit_s:.0f}s"
        elif ratio is not None and ratio >= budget:
            talk_blocked_reason = f"presupuesto palabra {ratio:.2f} ≥ {budget:.2f}"

        last = self._last_non_jingle(history)
        last_kind = last.kind if last is not None else None

        # Tipos de palabra permitidos (regla 3 + 4 + 5); time_signal solo vía regla 2
        allowed_talk: list[SegmentKind] = []
        if talk_blocked_reason is None:
            for kind in ALL_KINDS:
                if kind not in TALK_WEIGHTS or not has(kind):
                    continue
                if self._in_cooldown(kind, now, history):
                    continue
                if kind == "host_intro" and last_kind == "host_intro":
                    continue
                allowed_talk.append(kind)

        # Regla 6a: jingle de transición tras música
        if (
            last_kind == "music"
            and (not history or history[-1].kind != "jingle")
            and has("jingle")
            and not self._in_cooldown("jingle", now, history)
            and self.rng.random() < JINGLE_PROBABILITY
        ):
            return self._decide("jingle", f"jingle de transición ({slot.name})")

        # Regla 6b: palabra por pesos
        if allowed_talk:
            quiet = not self._talk_since(now - timedelta(minutes=HOST_INTRO_QUIET_MIN), history)
            weights: list[float] = []
            for kind in allowed_talk:
                w = TALK_WEIGHTS[kind]
                if kind == "host_intro" and last_kind == "music" and quiet:
                    w *= HOST_INTRO_BOOST
                weights.append(w)
            chosen = self._weighted_choice(allowed_talk, weights)
            ratio_txt = "n/a" if ratio is None else f"{ratio:.2f}"
            return self._decide(
                chosen,
                f"palabra permitida (ratio {ratio_txt} < {budget:.2f}, racha {run_s:.0f}s); "
                f"candidatos {allowed_talk} → {chosen} ({slot.name})",
            )

        # Regla 7: valores por defecto
        if has("music"):
            why = talk_blocked_reason or "sin palabra permitida"
            return self._decide("music", f"música: {why} ({slot.name})")

        for kind in ALL_KINDS:
            if has(kind) and not self._in_cooldown(kind, now, history):
                return self._decide(kind, f"sin música; {kind} disponible fuera de cooldown")

        for kind in ALL_KINDS:
            if has(kind):
                return self._decide(
                    kind, f"sin música; {kind} ignorando restricciones (evitar silencio)"
                )

        return self._decide(None, "nada disponible")  # pragma: no cover (inalcanzable)

    def explain(self) -> str:
        """Motivo legible de la última decisión (para logs y simulación)."""
        return self._last_reason

    # ── Internos ─────────────────────────────────────────────────────────────

    def _aware(self, t: datetime) -> datetime:
        """Asegura fecha aware; una naive se interpreta como hora local de la parrilla."""
        if t.tzinfo is None:
            return t.replace(tzinfo=self._tz)
        return t

    def _to_local(self, t: datetime) -> datetime:
        return self._aware(t).astimezone(self._tz)

    def _decide(self, kind: SegmentKind | None, reason: str) -> SegmentKind | None:
        self._last_reason = f"{kind}: {reason}"
        return kind

    def _cooldown_minutes(self, kind: SegmentKind) -> int:
        cd = self.grid.cooldowns_minutes
        match kind:
            case "factual":
                return cd.factual
            case "fiction":
                return cd.fiction
            case "jingle":
                return cd.jingle
            case "time_signal":
                return cd.time_signal
            case _:
                return 0

    def _in_cooldown(
        self, kind: SegmentKind, now: datetime, history: Sequence[PlayRecord]
    ) -> bool:
        """Regla 3: ¿empezó un registro de este tipo hace menos de N minutos?"""
        minutes = self._cooldown_minutes(kind)
        if minutes <= 0:
            return False
        limit = now - timedelta(minutes=minutes)
        return any(
            rec.kind == kind and self._aware(rec.started_at) > limit for rec in history
        )

    @staticmethod
    def _talk_run_seconds(history: Sequence[PlayRecord]) -> float:
        """Regla 4: duración de la racha final de palabra (los jingles son transparentes)."""
        total = 0.0
        for rec in reversed(history):
            if rec.kind == "jingle":
                continue
            if rec.kind not in TALK_KINDS:
                break
            total += rec.duration_s
        return total

    def _talk_ratio(self, now: datetime, history: Sequence[PlayRecord]) -> float | None:
        """Regla 5: proporción de palabra en la última hora; None si no hay antena."""
        window_start = now - BUDGET_WINDOW
        talk = 0.0
        total = 0.0
        for rec in history:
            start = self._aware(rec.started_at)
            end = start + timedelta(seconds=rec.duration_s)
            clipped = (min(end, now) - max(start, window_start)).total_seconds()
            if clipped <= 0:
                continue
            total += clipped
            if rec.kind in TALK_KINDS:
                talk += clipped
        if total <= 0:
            return None
        return talk / total

    def _talk_since(self, since: datetime, history: Sequence[PlayRecord]) -> bool:
        """¿Ha empezado algún segmento de palabra (sin contar señal horaria) desde `since`?"""
        return any(
            rec.kind in TALK_KINDS
            and rec.kind != "time_signal"
            and self._aware(rec.started_at) >= since
            for rec in history
        )

    @staticmethod
    def _last_non_jingle(history: Sequence[PlayRecord]) -> PlayRecord | None:
        for rec in reversed(history):
            if rec.kind != "jingle":
                return rec
        return None

    def _weighted_choice(
        self, kinds: Sequence[SegmentKind], weights: Sequence[float]
    ) -> SegmentKind:
        """Elección ponderada determinista con self.rng (un único random())."""
        r = self.rng.random() * sum(weights)
        acc = 0.0
        for kind, w in zip(kinds, weights, strict=True):
            acc += w
            if r < acc:
                return kind
        return kinds[-1]
