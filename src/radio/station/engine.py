"""
Motor de la emisora (§4.4): programa con ``radio.grid`` y alimenta un reproductor con cola.

La emisora es el mundo "siempre encendido" de §2: **solo programa y reproduce desde
disco** (invariante 2). Este módulo no importa productores, LLM, TTS ni nada de red;
lee ``segments`` y la parrilla y escribe ``play_log``. Si se cae internet, sigue.

Modelo de eventos
-----------------
El motor no tiene hilos ni bucles propios: reacciona a tres cosas.

1. **Eventos del reproductor** (``Started``/``Ended`` de un ``QueueingAudioBackend``):
   - ``Started`` → abre la fila de ``play_log`` (``log_play_start`` con kind y modo).
   - ``Ended`` → la cierra (``log_play_end``; ``skipped`` si fue ``skipped``/``error``).
     La palabra que acaba con ``eof`` pasa a ``retired`` (música, jingles y stingers
     siguen ``ready`` en rotación). Un archivo que da ``error`` sin llegar a sonar, o
     cuyo audio ya no existe (p. ej. la caché LRU de Tiny Desk lo borró estando en
     cola), pasa a ``quarantined``. Después se rellena la cola.
2. **Temporizador de interrupciones** (``rules.next_interrupt_at``): al llegar la hora
   se vuelve a programar desde ``now`` (ver *Interrupciones*).
3. **Reintento tras emergencia** (``emergency_retry_s``).

Quien lo conduce llama a ``tick()`` cuando vence ``next_wakeup()`` (y de vez en
cuando, para reconciliar tras un relanzamiento del reproductor). Con ``FakeClock`` y
``FakeEventBackend`` todo es síncrono y determinista (tests y ``radio simulate``); con
mpv, ``radio.station.service`` lo conduce desde el hilo principal.

Los eventos llegan a una bandeja (``inbox``) y se procesan de uno en uno (``drain``):
nunca hay reentrada aunque el backend emita eventos dentro de ``enqueue``/``skip``.
Con ``auto_drain=True`` (backend síncrono) el oyente vacía la bandeja él mismo; con
``auto_drain=False`` (mpv: los oyentes corren en el hilo lector) solo encola y avisa
con ``on_wake`` para que el hilo principal llame a ``drain()``.

Lookahead
---------
Se mantienen ``lookahead_units`` unidades pendientes por detrás de lo que suena
(§4.4: 2–3 en cola contando la que suena). Cada unidad se decide con
``grid.next_unit`` en su **hora proyectada de inicio** (fin previsto de lo encolado)
y con un ``SchedulerState`` que es el ``play_log`` real (lo emitido y lo que suena)
más lo planificado y aún no emitido, añadido con ``advance_state`` en sus horas
proyectadas. Así el scheduler cuenta cooldowns, presupuesto de charla, artista
anterior y §1.4 con lo que ya está en cola. El cursor del patrón avanza al planificar
(y retrocede si una interrupción descarta lo planificado). La palabra encolada se
quita del stock que ve el scheduler para no elegirla dos veces.

Interrupciones
--------------
A la hora de ``next_interrupt_at`` se pregunta a ``next_unit`` desde ``now`` con lo
emitido de verdad (sin lo planificado). Si devuelve ``interrupt=True``:

- si la primera unidad pendiente ya es esa y empezará dentro de su
  ``max_late_seconds``, no se toca nada (el *lookahead* ya lo había previsto);
- si no, se descartan los pendientes (``backend.clear_pending()``) y la unidad de
  interrupción pasa a ser la siguiente. Lo que suena:
  - si acaba dentro de ``max_late_seconds``, se deja terminar;
  - si no: la música se corta si ``station.interrupts.cut_music`` (por defecto sí);
    el bucle de emergencia y la palabra/jingles que harían llegar tarde la
    interrupción se cortan siempre (lo cortado no se retira: sigue ``ready``);
  - con ``cut_music: false`` y una canción que acaba tarde, la interrupción espera,
    pero si ya no llegaría dentro de ``max_late_seconds`` se omite (una señal horaria
    tardía sería falsa) y se registra en el log.

Escalera de degradación (§8) y nunca silencio (inv. 3)
------------------------------------------------------
Si ``next_unit`` devuelve el peldaño 5, falla (excepción) o todas las opciones
tienen el audio ausente, y no hay nada sonando ni en cola, se encola el bucle de
emergencia (``playout.emergency_dir``, p. ej. ``assets/emergency/emergency_loop.wav``),
registrado en ``play_log`` con kind ``emergency`` y ``segment_id`` NULL. Se reintenta
programar a los ``emergency_retry_s`` segundos (y al acabar cada vuelta del bucle); en
cuanto hay algo que emitir, se corta el bucle y suena.

Watchdog
--------
El backend relanza el reproductor si muere (``restarts``). En cada ``tick()`` se
comprueba: tras un relanzamiento se reconcilia la cola (si el backend perdió
pendientes, se vuelven a encolar) y se registra en el log.
"""

from __future__ import annotations

import logging
import queue
import random
import wave
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from radio.core.clock import Clock
from radio.core.config import GridConfig, RadioConfig
from radio.core.models import Segment, Status, StockView
from radio.core.store import DB
from radio.grid.rules import next_interrupt_at, resolve_mode
from radio.grid.scheduler import (
    RUNG_EMERGENCY,
    PlayUnit,
    SchedulerState,
    advance_pattern,
    advance_state,
    history_horizon,
    next_unit,
)
from radio.music.library import AUDIO_EXTENSIONS
from radio.providers.audio.base import QueueingAudioBackend
from radio.providers.audio.events import Ended, EndReason, PlayerEvent, Started
from radio.station.queue import EMERGENCY_KIND, AirQueue, QueueItem

logger = logging.getLogger(__name__)

# Intentos de programación por hueco cuando faltan audios en disco
MAX_PLAN_ATTEMPTS = 6

# Kinds que siguen "ready" tras emitirse (rotación)
REUSABLE_KINDS: frozenset[str] = frozenset({"music", "jingle", "stinger"})

# Duración supuesta de un audio de emergencia que no es WAV
DEFAULT_EMERGENCY_S = 30.0


@dataclass(frozen=True)
class AiredItem:
    """Un archivo que sonó (para la línea de tiempo de ``radio simulate`` y los tests)."""
    kind: str
    title: str
    segment_id: str | None
    started_at: datetime
    ended_at: datetime
    end_reason: EndReason
    rung: int
    reason: str
    interrupt: bool = False
    cut: bool = False

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


@dataclass
class EngineStats:
    """Contadores de la emisora desde que arrancó."""
    units_started: Counter[int] = field(default_factory=Counter)   # peldaño → unidades
    interrupts: int = 0          # veces que una interrupción se adelantó a la cola
    interrupts_omitted: int = 0  # interrupciones omitidas (cut_music=false y llegaban tarde)
    music_cuts: int = 0          # canciones cortadas para dar paso a una interrupción
    cuts: int = 0                # cualquier archivo cortado por una interrupción
    emergencies: int = 0         # vueltas del bucle de emergencia encoladas
    quarantined: int = 0         # segmentos puestos en cuarentena
    restarts: int = 0            # relanzamientos del reproductor vistos


class StationEngine:
    """Programación con lookahead sobre un ``QueueingAudioBackend`` (ver docstring del módulo)."""

    def __init__(
        self,
        db: DB,
        grid: GridConfig,
        backend: QueueingAudioBackend,
        clock: Clock,
        *,
        mode: str = "default",
        rng: random.Random | None = None,
        lookahead_units: int = 2,
        cut_music: bool = True,
        emergency_dir: Path | None = Path("assets/emergency"),
        emergency_retry_s: float = 30.0,
        verify_files: bool = True,
        history_window: timedelta | None = None,
        auto_drain: bool = True,
        on_wake: Callable[[], None] | None = None,
        on_aired: Callable[[AiredItem], None] | None = None,
    ) -> None:
        self.db = db
        self.grid = grid
        self.backend = backend
        self.clock = clock
        self.tz = ZoneInfo(grid.timezone)
        self.mode = mode
        self.rng = rng if rng is not None else random.Random()
        self.lookahead_units = max(1, lookahead_units)
        self.cut_music = cut_music
        self.emergency_dir = emergency_dir
        self.emergency_retry = timedelta(seconds=emergency_retry_s)
        self.verify_files = verify_files
        self.history_window = history_window if history_window is not None else history_horizon(grid)
        self.auto_drain = auto_drain
        self.on_wake = on_wake
        self.on_aired = on_aired

        self.queue = AirQueue()
        self.stats = EngineStats()
        self.pattern_pos: Mapping[str, int] = MappingProxyType({})
        self.interrupt_at: datetime | None = None
        self.retry_at: datetime | None = None
        self._inbox: queue.SimpleQueue[PlayerEvent] = queue.SimpleQueue()
        self._busy = False
        self._started = False
        self._stopping = False
        self._unit_no = 0
        self._emergency_index = 0
        self._emergency_failed_at: datetime | None = None
        self._restarts_seen = 0
        self._cache: dict[str, Segment] = {}

    @classmethod
    def from_config(
        cls,
        config: RadioConfig,
        db: DB,
        backend: QueueingAudioBackend,
        clock: Clock,
        **kwargs: Any,
    ) -> StationEngine:
        """
        Motor con los ajustes de station.yaml (``playout``, ``interrupts``) y grid.yaml;
        ``kwargs`` completa o sustituye (``mode``, ``rng``, ``verify_files``...).
        """
        playout = config.station.playout
        kwargs.setdefault("lookahead_units", playout.lookahead_units)
        kwargs.setdefault("cut_music", config.station.interrupts.cut_music)
        kwargs.setdefault("emergency_dir", resolve_emergency_dir(config))
        kwargs.setdefault("emergency_retry_s", playout.emergency_retry_s)
        return cls(db, config.grid, backend, clock, **kwargs)

    # ── Ciclo de vida ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Se suscribe al backend, programa el temporizador y llena la cola."""
        if self._started:
            return
        self._started = True
        self._restarts_seen = self.backend.restarts
        self.backend.add_listener(self._on_backend_event)
        now = self._now()
        logger.info("Emisora en marcha (modo %s, lookahead %d)", self.mode, self.lookahead_units)
        with self._exclusive():
            self._schedule_interrupt(now)
            self._refill(now)

    def stop(self) -> None:
        """Deja de programar. Los eventos que aún lleguen (cierre del backend) se registran."""
        self._stopping = True
        self.interrupt_at = self.retry_at = None

    @property
    def stopping(self) -> bool:
        return self._stopping

    def next_wakeup(self) -> datetime | None:
        """Próximo instante en que hay que llamar a ``tick()`` (o None)."""
        times = [t for t in (self.interrupt_at, self.retry_at) if t is not None]
        return min(times) if times else None

    def tick(self) -> None:
        """Procesa eventos pendientes, temporizadores vencidos y relanzamientos del reproductor."""
        with self._exclusive():
            self._process_inbox()
            if self._stopping:
                return
            now = self._now()
            self._reconcile(now)
            if self.interrupt_at is not None and now >= self.interrupt_at:
                self.interrupt_at = None
                self._on_interrupt(now)
                self._schedule_interrupt(now)
            if self.retry_at is not None and now >= self.retry_at:
                self.retry_at = None
                self._refill(now, retry=True)

    def drain(self) -> None:
        """Procesa los eventos del reproductor que esperan en la bandeja."""
        with self._exclusive():
            self._process_inbox()

    # ── Eventos ──────────────────────────────────────────────────────────────

    def _on_backend_event(self, event: PlayerEvent) -> None:
        """Oyente del backend: puede llamarse desde otro hilo (mpv) → solo encola."""
        self._inbox.put(event)
        if self.on_wake is not None:
            self.on_wake()
        if self.auto_drain and not self._busy:
            self.drain()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Sección sin reentrada: los eventos que lleguen dentro se procesan al salir."""
        if self._busy:
            yield
            return
        self._busy = True
        try:
            yield
            self._process_inbox()
        finally:
            self._busy = False

    def _process_inbox(self) -> None:
        while True:
            try:
                event = self._inbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(event)
            except Exception:
                # La emisora nunca debe caerse por un evento (§2)
                logger.exception("Error procesando %r", event)

    def _handle(self, event: PlayerEvent) -> None:
        if isinstance(event, Started):
            self._on_started(event)
        elif isinstance(event, Ended):
            self._on_ended(event)

    def _on_started(self, event: Started) -> None:
        item = self.queue.on_started(event.path)
        if item is None:
            return
        item.started_at = event.at
        seg_id = item.segment.id if item.segment is not None else None
        item.play_id = self.db.log_play_start(seg_id, item.kind, self.mode, event.at)
        unit = item.unit
        if unit is None:
            self.stats.units_started[RUNG_EMERGENCY] += 1
        elif item.segment is unit.segments[0]:
            self.stats.units_started[unit.rung] += 1
        logger.info(
            "En antena %s [%s] %s (%.0f s) — peldaño %s — %s",
            event.at.astimezone(self.tz).strftime("%H:%M:%S"), item.kind, item.title,
            item.duration_s, unit.rung if unit else RUNG_EMERGENCY,
            unit.reason if unit else "bucle de emergencia",
        )
        # Lo que empieza deja de estar pendiente: se repone el lookahead
        self._refill(self._now())

    def _on_ended(self, event: Ended) -> None:
        item = self.queue.on_ended(event.path)
        now = self._now()
        if item is None:
            self._refill(now)
            return
        if item.play_id is not None:
            self.db.log_play_end(item.play_id, event.at, skipped=event.reason != "eof")
        seg = item.segment
        if seg is not None:
            if event.reason == "eof" and seg.kind not in REUSABLE_KINDS:
                self._set_status(seg, "retired")
            elif event.reason == "error" and (item.started_at is None or not seg.path.is_file()):
                # No llegó a sonar (audio ausente o ilegible): fuera de la rotación
                logger.warning("Audio de %s (%s) no reproducible: %s → 'quarantined'",
                               seg.id, seg.title, seg.path)
                self._quarantine(seg)
        elif event.reason == "error":
            logger.error("El bucle de emergencia %s no se pudo reproducir", item.path)
            self._emergency_failed_at = now
            self.retry_at = now + self.emergency_retry
        if item.cut:
            self.stats.cuts += 1
            if item.kind == "music":
                self.stats.music_cuts += 1
        if item.started_at is not None and self.on_aired is not None:
            unit = item.unit
            self.on_aired(AiredItem(
                kind=item.kind, title=item.title, segment_id=seg.id if seg else None,
                started_at=item.started_at, ended_at=event.at, end_reason=event.reason,
                rung=unit.rung if unit else RUNG_EMERGENCY,
                reason=unit.reason if unit else "bucle de emergencia",
                interrupt=unit.interrupt if unit else False, cut=item.cut,
            ))
        self._refill(now)

    # ── Programación ─────────────────────────────────────────────────────────

    def _refill(self, now: datetime, *, retry: bool = False) -> None:
        """Llena la cola hasta ``lookahead_units`` unidades pendientes."""
        if self._stopping or not self._started:
            return
        enqueued = False
        while self.queue.pending_units() < self.lookahead_units:
            unit = self._plan(now)
            if unit.is_emergency:
                self._on_emergency(now, unit)
                break
            self._push_unit(unit)
            enqueued = True
        cur = self.queue.current
        if retry and enqueued and cur is not None and cur.is_emergency:
            logger.info("Vuelve a haber programación: se corta el bucle de emergencia")
            cur.cut = True
            self.backend.skip()

    def _plan(self, now: datetime) -> PlayUnit:
        """Decide la próxima unidad en la hora proyectada de inicio."""
        for _ in range(MAX_PLAN_ATTEMPTS):
            at = self.queue.tail_time(now)
            try:
                state = self._state(now, include_pending=True)
                unit = next_unit(state, self._stock(at), at, self.mode, self.rng)
            except Exception as exc:
                logger.exception("El scheduler ha fallado")
                return PlayUnit((), f"error del scheduler: {exc}", RUNG_EMERGENCY, mode=self.mode)
            if unit.is_emergency or not self.verify_files:
                return unit
            missing = [s for s in unit.segments if not s.path.is_file()]
            if not missing:
                return unit
            for seg in missing:
                logger.warning("Audio no encontrado para %s (%s): %s → 'quarantined'",
                               seg.id, seg.title, seg.path)
                self._quarantine(seg)
        return PlayUnit(
            (), f"audios ausentes tras {MAX_PLAN_ATTEMPTS} intentos", RUNG_EMERGENCY, mode=self.mode
        )

    def _push_unit(self, unit: PlayUnit) -> None:
        self._unit_no += 1
        self.pattern_pos = advance_pattern(self.pattern_pos, unit)
        items = [
            QueueItem(path=seg.path, kind=seg.kind, duration_s=seg.duration_s,
                      unit_no=self._unit_no, segment=seg, unit=unit)
            for seg in unit.segments
        ]
        self._enqueue(items)

    def _enqueue(self, items: list[QueueItem]) -> None:
        # Primero el espejo y luego el backend: sus eventos pueden llegar dentro de enqueue
        for item in items:
            self.queue.push(item)
        for item in items:
            self.backend.enqueue(item.path)

    def _state(self, now: datetime, *, include_pending: bool) -> SchedulerState:
        """``play_log`` reciente (lo emitido y lo que suena) + lo planificado sin emitir."""
        history = tuple(self.db.list_play_log(since=now - self.history_window))
        segments: dict[str, Segment] = {}
        for entry in history:
            sid = entry.segment_id
            if sid is None or sid in segments:
                continue
            seg = self._cache.get(sid) or self.db.get_segment(sid)
            if seg is not None:
                self._cache[sid] = seg
                segments[sid] = seg
        state = SchedulerState(
            grid=self.grid, history=history, pattern_pos=self.pattern_pos,
            segments=MappingProxyType(segments),
        )
        if include_pending:
            for items, start in self.queue.pending_by_unit(now):
                segs = tuple(i.segment for i in items if i.segment is not None)
                if segs:
                    # Sin pattern_key: el cursor ya avanzó al planificar
                    state = advance_state(
                        state, PlayUnit(segs, "planificada", 1, mode=self.mode), start
                    )
        return state

    def _stock(self, at: datetime) -> StockView:
        """Stock emitible en ``at`` sin la palabra que ya está en cola."""
        queued = {
            i.segment.id for i in self.queue.items()
            if i.segment is not None and i.segment.kind not in REUSABLE_KINDS
        }
        view = self.db.stock_view(at)
        if not queued:
            return view
        return StockView(MappingProxyType({
            kind: tuple(s for s in segs if s.id not in queued)
            for kind, segs in view.by_kind.items()
        }))

    # ── Interrupciones ───────────────────────────────────────────────────────

    def _schedule_interrupt(self, now: datetime) -> None:
        self.interrupt_at = next_interrupt_at(self.grid, now, self.mode)

    def _max_late_s(self, unit: PlayUnit) -> float:
        _, cfg = resolve_mode(self.grid, self.mode)
        kinds = {s.kind for s in unit.segments}
        return max((r.max_late_seconds for r in cfg.interrupts if r.kind in kinds), default=0.0)

    def _on_interrupt(self, now: datetime) -> None:
        """Ha llegado la hora de una regla de interrupción: se le da paso si toca."""
        try:
            state = self._state(now, include_pending=False)
            unit = next_unit(state, self._stock(now), now, self.mode, self.rng)
        except Exception:
            logger.exception("El scheduler ha fallado al comprobar la interrupción")
            return
        if not unit.interrupt or unit.is_emergency:
            logger.debug("Hora de interrupción sin nada que interrumpir: %s", unit.reason)
            return
        late = timedelta(seconds=self._max_late_s(unit))
        deadline = now + late
        groups = self.queue.pending_by_unit(now)
        if groups:
            first, start = groups[0]
            if [i.segment for i in first] == list(unit.segments) and start <= deadline:
                logger.info("Interrupción ya en cola a tiempo: %s", unit.reason)
                return

        cur = self.queue.current
        cut = False
        if cur is not None and self.queue.current_end(now) > deadline:
            if cur.kind != "music" or self.cut_music:
                cut = True
            else:
                # cut_music: false → espera; se omite si ya no llegaría a tiempo
                self.stats.interrupts_omitted += 1
                logger.warning(
                    "Interrupción omitida (cut_music: false y %s acaba tarde): %s",
                    cur.title, unit.reason,
                )
                return
        self._drop_pending()
        self._unit_no += 1
        self._enqueue([
            QueueItem(path=seg.path, kind=seg.kind, duration_s=seg.duration_s,
                      unit_no=self._unit_no, segment=seg, unit=unit)
            for seg in unit.segments
        ])
        self.stats.interrupts += 1
        logger.info("Interrupción: %s%s", unit.reason,
                    f" (se corta {cur.title})" if cut and cur is not None else "")
        if cut and cur is not None:
            cur.cut = True
            self.backend.skip()
        self._refill(now)

    def _drop_pending(self) -> None:
        """Descarta lo planificado sin emitir (y deshace su avance del patrón)."""
        dropped = self.queue.drop_pending()
        self.backend.clear_pending()
        pos = dict(self.pattern_pos)
        seen: set[int] = set()
        for item in dropped:
            unit = item.unit
            if item.unit_no in seen or unit is None or unit.pattern_key is None:
                continue
            seen.add(item.unit_no)
            pos[unit.pattern_key] = max(0, pos.get(unit.pattern_key, 0) - 1)
        self.pattern_pos = MappingProxyType(pos)

    # ── Emergencia ───────────────────────────────────────────────────────────

    def _on_emergency(self, now: datetime, unit: PlayUnit) -> None:
        self.retry_at = now + self.emergency_retry
        if self.queue.current is not None or self.queue.pending:
            return          # aún suena algo: se reintenta antes de que acabe
        logger.warning("Peldaño 5 (%s): %s", unit.mode, unit.reason)
        failed = self._emergency_failed_at
        if failed is not None and now - failed < self.emergency_retry:
            return
        path = self._emergency_file()
        if path is None:
            logger.error("No hay audio de emergencia en %s: SILENCIO", self.emergency_dir)
            return
        self._unit_no += 1
        self.stats.emergencies += 1
        self._enqueue([QueueItem(path=path, kind=EMERGENCY_KIND,
                                 duration_s=audio_duration(path), unit_no=self._unit_no)])

    def _emergency_file(self) -> Path | None:
        """Audio de ``emergency_dir`` (rotando si hay varios)."""
        d = self.emergency_dir
        if d is None or not d.is_dir():
            return None
        files = sorted(
            p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and not p.name.startswith(".")
        )
        if not files:
            return None
        path = files[self._emergency_index % len(files)]
        self._emergency_index += 1
        return path

    # ── Watchdog ─────────────────────────────────────────────────────────────

    def _reconcile(self, now: datetime) -> None:
        """Tras un relanzamiento del reproductor, alinea la cola del backend con la nuestra."""
        restarts = self.backend.restarts
        if restarts == self._restarts_seen:
            return
        self.stats.restarts += restarts - self._restarts_seen
        self._restarts_seen = restarts
        logger.warning("El reproductor se ha relanzado (%d en total): reconciliando la cola",
                       restarts)
        cur = self.queue.current
        if cur is not None and self.backend.current() is None and self.backend.queued() == 0:
            # Lo que sonaba se perdió sin su Ended: se cierra como error
            self.queue.current = None
            if cur.play_id is not None:
                self.db.log_play_end(cur.play_id, now, skipped=True)
        if self.backend.queued() != len(self.queue.pending):
            logger.warning("Cola del reproductor desalineada (%d frente a %d): se reenvía",
                           self.backend.queued(), len(self.queue.pending))
            self.backend.clear_pending()
            for item in self.queue.pending:
                self.backend.enqueue(item.path)
        self._refill(now)

    # ── Utilidades ───────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        t = self.clock.now()
        return t.replace(tzinfo=self.tz) if t.tzinfo is None else t

    def _set_status(self, seg: Segment, status: Status) -> None:
        self.db.update_segment_status(seg.id, status)
        self._cache.pop(seg.id, None)

    def _quarantine(self, seg: Segment) -> None:
        self._set_status(seg, "quarantined")
        self.stats.quarantined += 1


def resolve_emergency_dir(config: RadioConfig) -> Path:
    """
    ``playout.emergency_dir``; si es relativa y no existe desde el directorio actual,
    se busca junto a ``config/`` (el checkout), para poder lanzar desde otra carpeta.
    """
    path = Path(config.station.playout.emergency_dir)
    if path.is_absolute() or path.is_dir():
        return path
    beside = config.config_dir.parent / path
    return beside if beside.is_dir() else path


def audio_duration(path: Path) -> float:
    """Duración de un WAV por su cabecera; otros formatos, ``DEFAULT_EMERGENCY_S``."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return DEFAULT_EMERGENCY_S
