"""
Playout de Radio Parra: convierte las unidades del scheduler (``radio.grid``) en audio.

Cada llamada a ``Playout.step()`` emite exactamente una ``PlayUnit`` (bloqueante):

1. Construye el ``SchedulerState`` desde la BD: ``play_log`` de los últimos
   ``history_window`` (por defecto ``history_horizon(grid)``), los segmentos a los que
   apunta y el cursor del patrón (que vive en memoria: tras reiniciar empieza de 0).
2. Pide ``next_unit`` con el stock emitible (``StockView``: ready y sin caducar).
3. Si algún audio de la unidad no existe en disco (``verify_files``), ese segmento
   pasa a ``quarantined`` y se vuelve a preguntar (máximo ``MAX_ATTEMPTS``).
4. Emite los segmentos de la unidad uno detrás de otro: abre la emisión en
   ``play_log`` (con el ``mode``), llama a ``audio.play(path)`` y la cierra con la hora
   real de fin (``clock.now()`` tras volver) y ``skipped`` si se cortó. Si se corta un
   segmento, el resto de la unidad no se emite.

Tras emitir, la palabra pasa a ``retired``; ``music``, ``jingle`` y ``stinger`` siguen
``ready`` (rotación). Un segmento cortado sigue ``ready``.

Peldaño 5 (unidad vacía): se reproduce, sin registrarlo, un audio de ``emergency_dir``
si lo hay, y ``step()`` devuelve ``None``. ``last_unit`` guarda siempre la última
unidad decidida (también la de emergencia) y ``last_emergency`` el audio usado.

La emisora definitiva (cola con lookahead, cortes por interrupción) se construirá
sobre ``radio.grid`` directamente; este playout secuencial es el de Fase 1 y el que
usa ``radio simulate``.

Simulación
----------
El Playout no calcula duraciones: mide el tiempo real con ``clock.now()`` antes y
después de ``audio.play``. En simulación, ``SimAudioBackend`` (``radio.sim``) avanza el
``FakeClock`` la duración del segmento.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from radio.core.clock import Clock
from radio.core.config import GridConfig
from radio.core.models import Segment, SegmentKind, Status
from radio.core.store import DB
from radio.grid.scheduler import (
    RUNG_EMERGENCY,
    PlayUnit,
    SchedulerState,
    advance_pattern,
    history_horizon,
    next_unit,
)
from radio.music.library import AUDIO_EXTENSIONS
from radio.providers.audio.base import AudioBackend

logger = logging.getLogger(__name__)

# Intentos máximos de decisión por paso (audios que faltan)
MAX_ATTEMPTS = 6

# Tipos que siguen "ready" tras emitirse (se reutilizan en rotación)
REUSABLE_KINDS: frozenset[str] = frozenset({"music", "jingle", "stinger"})


@dataclass(frozen=True)
class AiredSegment:
    """Un segmento emitido dentro de una unidad."""
    segment_id: str
    kind: SegmentKind
    title: str
    started_at: datetime
    duration_s: float
    skipped: bool = False


@dataclass(frozen=True)
class PlayOutcome:
    """
    Resultado de un paso: la unidad emitida, por qué y cada segmento que sonó.
    ``segment_id``/``kind``/``title``/``started_at``/``duration_s`` describen el
    segmento principal (el último de la unidad, p. ej. la música tras su intro).
    """
    unit: PlayUnit
    aired: tuple[AiredSegment, ...]

    @property
    def reason(self) -> str:
        return self.unit.reason

    @property
    def rung(self) -> int:
        return self.unit.rung

    @property
    def main(self) -> AiredSegment:
        return self.aired[-1]

    @property
    def segment_id(self) -> str:
        return self.main.segment_id

    @property
    def kind(self) -> SegmentKind:
        return self.main.kind

    @property
    def title(self) -> str:
        return self.main.title

    @property
    def started_at(self) -> datetime:
        return self.aired[0].started_at

    @property
    def duration_s(self) -> float:
        return sum(a.duration_s for a in self.aired)


class Playout:
    """Bucle de emisión: ``next_unit`` → verificación de audio → emisión → registro."""

    def __init__(
        self,
        db: DB,
        grid: GridConfig,
        audio: AudioBackend,
        clock: Clock,
        *,
        tz: str | None = None,
        mode: str = "default",
        rng: random.Random | None = None,
        verify_files: bool = True,
        history_window: timedelta | None = None,
        emergency_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.grid = grid
        self.audio = audio
        self.clock = clock
        self.tz = ZoneInfo(tz or grid.timezone)
        self.mode = mode
        self.rng = rng if rng is not None else random.Random()
        self.verify_files = verify_files
        self.history_window = history_window if history_window is not None else history_horizon(grid)
        self.emergency_dir = emergency_dir
        self.pattern_pos: Mapping[str, int] = MappingProxyType({})
        # Última unidad decidida (también la de emergencia) y audio de emergencia usado
        self.last_unit: PlayUnit | None = None
        self.last_emergency: Path | None = None
        self._emergency_index = 0
        self._interrupted = False
        self._cache: dict[str, Segment] = {}

    # ── API pública ──────────────────────────────────────────────────────────

    def state(self, now: datetime) -> SchedulerState:
        """Estado del scheduler reconstruido desde la BD en ``now``."""
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
        return SchedulerState(
            grid=self.grid,
            history=history,
            pattern_pos=self.pattern_pos,
            segments=MappingProxyType(segments),
        )

    def decide(self, now: datetime | None = None) -> PlayUnit:
        """Decide la próxima unidad sin emitirla (sin verificar audios)."""
        now = self._now() if now is None else now
        return next_unit(self.state(now), self.db.stock_view(now), now, self.mode, self.rng)

    def step(self) -> PlayOutcome | None:
        """Emite una unidad (bloqueante). ``None`` si tocó emergencia (peldaño 5)."""
        self.last_emergency = None
        now = self._now()
        state = self.state(now)
        for _ in range(MAX_ATTEMPTS):
            stock = self.db.stock_view(now)
            unit = next_unit(state, stock, now, self.mode, self.rng)
            if unit.is_emergency or self._all_files_present(unit):
                break
        else:
            unit = PlayUnit(
                (), f"audios ausentes tras {MAX_ATTEMPTS} intentos", RUNG_EMERGENCY, mode=self.mode
            )
        self.last_unit = unit
        if unit.is_emergency:
            logger.warning("Peldaño 5 (%s): %s", unit.mode, unit.reason)
            self._play_emergency()
            return None
        outcome = self._air(unit)
        self.pattern_pos = advance_pattern(self.pattern_pos, unit)
        return outcome

    def interrupt(self) -> None:
        """Corta el audio en curso (p. ej. al recibir SIGINT/SIGTERM)."""
        self._interrupted = True
        self.audio.skip()

    # ── Internos ─────────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        t = self.clock.now()
        return t.replace(tzinfo=self.tz) if t.tzinfo is None else t

    def _all_files_present(self, unit: PlayUnit) -> bool:
        """Pone en cuarentena los segmentos sin audio; True si no faltaba ninguno."""
        if not self.verify_files:
            return True
        ok = True
        for seg in unit.segments:
            if not seg.path.is_file():
                logger.warning(
                    "Audio no encontrado para %s (%s): %s → status 'quarantined'",
                    seg.id, seg.title, seg.path,
                )
                self._set_status(seg.id, "quarantined")
                ok = False
        return ok

    def _set_status(self, seg_id: str, status: Status) -> None:
        self.db.update_segment_status(seg_id, status)
        self._cache.pop(seg_id, None)

    def _air(self, unit: PlayUnit) -> PlayOutcome:
        aired: list[AiredSegment] = []
        for seg in unit.segments:
            started = self._now()
            play_id = self.db.log_play_start(seg.id, seg.kind, self.mode, started)
            logger.info(
                "En antena %s [%s] %s (%.0f s) — peldaño %d — %s",
                started.astimezone(self.tz).strftime("%H:%M:%S"),
                seg.kind, seg.title, seg.duration_s, unit.rung, unit.reason,
            )
            self._interrupted = False
            try:
                self.audio.play(seg.path)
            except Exception:
                logger.exception("Error reproduciendo %s", seg.path)
                self._interrupted = True
            interrupted = self._interrupted
            self.db.log_play_end(play_id, self._now(), skipped=interrupted)
            aired.append(AiredSegment(
                segment_id=seg.id, kind=seg.kind, title=seg.title, started_at=started,
                duration_s=seg.duration_s, skipped=interrupted,
            ))
            # Un segmento de palabra interrumpido sigue "ready" para poder emitirse después
            if interrupted:
                break
            if seg.kind not in REUSABLE_KINDS:
                self._set_status(seg.id, "retired")
        return PlayOutcome(unit=unit, aired=tuple(aired))

    def _play_emergency(self) -> None:
        """Reproduce un audio de emergencia (rotando) si hay alguno disponible."""
        if self.emergency_dir is None or not self.emergency_dir.is_dir():
            return
        files = sorted(
            p for p in self.emergency_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and not p.name.startswith(".")
        )
        if not files:
            return
        path = files[self._emergency_index % len(files)]
        self._emergency_index += 1
        logger.warning("Nada que emitir: audio de emergencia %s", path.name)
        self.last_emergency = path
        self.audio.play(path)

