"""
Playout de Radio Parra: convierte las decisiones del scheduler en audio en antena.

Cada llamada a ``Playout.step()`` emite exactamente un segmento (bloqueante):

1. Construye el historial reciente (``PlayRecord``) a partir de ``play_log``.
2. Pregunta al scheduler qué tipo toca (``next_kind``) con el stock emitible
   (``StockView``: ready y sin caducar).
3. Elige un segmento concreto de ese tipo (reglas de selección, abajo).
4. Abre la emisión en ``play_log`` (con el ``mode`` de la emisora), llama a
   ``audio.play(path)`` y la cierra con la hora real de fin (``clock.now()`` tras
   volver de ``play``) y ``skipped`` si se cortó.

Reglas de selección
-------------------
- ``time_signal``: solo vale la señal emitible etiquetada con la hora local en curso
  (``hour:YYYY-MM-DDTHH`` en ``meta["tags"]``). Si no hay ninguna, la señal horaria
  se considera no disponible y se vuelve a preguntar al scheduler sin ella.
- ``music``: se excluye el artista de la última canción emitida (nunca dos canciones
  seguidas del mismo artista). Además, si hay preparada la señal de la próxima hora,
  se prefieren canciones que terminen antes de que se cierre su ventana
  (minutos 0–4), para no pisar la señal horaria. Si no hay candidatas, se relajan
  las restricciones en este orden: primero la ventana horaria, luego el artista.
- Resto de tipos: ``pick_ready(kind)`` (nunca emitido primero, luego el más antiguo).
- Si el audio no existe en disco (``verify_files``), el segmento pasa a
  ``quarantined`` y se reintenta la selección (máximo ``MAX_ATTEMPTS`` por paso).

Tras emitir, los segmentos de palabra pasan a ``retired``; ``music`` y ``jingle`` se
quedan en ``ready`` porque son reutilizables (rotación).

Si no hay nada que emitir, se reproduce (sin registrarlo como segmento) un audio de
``emergency_dir`` si lo hay, y ``step()`` devuelve ``None``.

Simulación
----------
El Playout no calcula duraciones: mide el tiempo real con ``clock.now()`` antes y
después de ``audio.play``. En simulación, el backend de audio (``SimAudioBackend``
en ``radio.sim``) avanza el ``FakeClock`` la duración del segmento, de modo que las
filas de ``play_log`` quedan con ``started_at``/``ended_at`` correctos en tiempo simulado.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from radio.core.clock import Clock
from radio.core.models import PlayLogEntry, Segment, SegmentKind
from radio.core.scheduler import TIME_SIGNAL_WINDOW_MIN, PlayRecord, Scheduler
from radio.core.store import DB
from radio.music.library import AUDIO_EXTENSIONS
from radio.producers.time_signal import hour_tag
from radio.providers.audio.base import AudioBackend

logger = logging.getLogger(__name__)

# Intentos máximos de selección por paso (tipos descartados + audios que faltan)
MAX_ATTEMPTS = 6

# Tipos que siguen "ready" tras emitirse (se reutilizan en rotación)
REUSABLE_KINDS: frozenset[str] = frozenset({"music", "jingle"})

# Margen para que una canción termine estrictamente dentro de la ventana de la señal
_DEADLINE_MARGIN_S = 1.0

ARTIST_PREFIX = "artist:"


@dataclass(frozen=True)
class PlayOutcome:
    """Resultado de un paso de playout: qué se ha emitido y por qué."""
    segment_id: str
    kind: SegmentKind
    title: str
    started_at: datetime
    duration_s: float
    reason: str


class Playout:
    """Bucle de emisión: scheduler → selección de segmento → audio → registro en BD."""

    def __init__(
        self,
        db: DB,
        scheduler: Scheduler,
        audio: AudioBackend,
        clock: Clock,
        *,
        tz: str = "Europe/Madrid",
        mode: str = "default",
        verify_files: bool = True,
        history_window_min: int = 120,
        emergency_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.scheduler = scheduler
        self.audio = audio
        self.clock = clock
        self.tz = ZoneInfo(tz)
        self.mode = mode
        self.verify_files = verify_files
        self.history_window = timedelta(minutes=history_window_min)
        self.emergency_dir = emergency_dir
        # Último audio de emergencia emitido en el paso actual (None si no hubo)
        self.last_emergency: Path | None = None
        self._emergency_index = 0
        self._interrupted = False

    # ── API pública ──────────────────────────────────────────────────────────

    def step(self) -> PlayOutcome | None:
        """Emite un segmento (bloqueante). ``None`` si no había nada que emitir."""
        self.last_emergency = None
        now = self._now()
        plays = self._recent_plays(now)
        history = [self._to_record(p) for p in plays]
        discarded: set[str] = set()

        for _ in range(MAX_ATTEMPTS):
            stock = self.db.stock_view(now)
            available = {k: stock.count(k) for k in stock.kinds() if k not in discarded}
            kind = self.scheduler.next_kind(now, history, available)
            if kind is None:
                break
            reason = self.scheduler.explain()
            seg = self._select(kind, now, plays)
            if seg is None:
                logger.info("Sin segmento válido de tipo %s; se descarta en este paso", kind)
                discarded.add(kind)
                continue
            if self.verify_files and not seg.path.is_file():
                logger.warning(
                    "Audio no encontrado para %s (%s): %s → status 'quarantined'",
                    seg.id, seg.title, seg.path,
                )
                self.db.update_segment_status(seg.id, "quarantined")
                continue
            return self._air(seg, kind, reason)

        self._play_emergency()
        return None

    def interrupt(self) -> None:
        """Corta el audio en curso (p. ej. al recibir SIGINT/SIGTERM)."""
        self._interrupted = True
        self.audio.skip()

    # ── Historial ────────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        return self._aware(self.clock.now())

    def _aware(self, t: datetime) -> datetime:
        """Fecha aware; una naive se interpreta como hora local de la emisora."""
        return t.replace(tzinfo=self.tz) if t.tzinfo is None else t

    def _recent_plays(self, now: datetime) -> list[PlayLogEntry]:
        """Emisiones empezadas dentro de la ventana de historial (más antigua primero)."""
        return self.db.list_play_log(since=now - self.history_window)

    @staticmethod
    def _to_record(play: PlayLogEntry) -> PlayRecord:
        """PlayRecord con la duración real si la emisión está cerrada."""
        duration = play.duration_s or 0.0
        if play.ended_at is not None:
            duration = (play.ended_at - play.started_at).total_seconds()
        return PlayRecord(kind=play.kind, started_at=play.started_at, duration_s=duration)

    # ── Selección ────────────────────────────────────────────────────────────

    def _select(
        self, kind: SegmentKind, now: datetime, plays: Sequence[PlayLogEntry]
    ) -> Segment | None:
        match kind:
            case "time_signal":
                return self._ready_time_signal(now.astimezone(self.tz), now)
            case "music":
                return self._select_music(now, plays)
            case _:
                return self.db.pick_ready(kind, now=now)

    def _ready_time_signal(self, hour: datetime, now: datetime) -> Segment | None:
        """Señal horaria emitible en `now` etiquetada con la hora local de `hour` (o None)."""
        seg = self.db.find_by_meta("time_signal", "tags", hour_tag(hour), status="ready")
        return seg if seg is not None and seg.is_live(now) else None

    def _select_music(self, now: datetime, plays: Sequence[PlayLogEntry]) -> Segment | None:
        exclude = self._last_music_artist_tags(plays)
        max_dur = self._music_deadline_s(now)
        attempts: list[tuple[list[str], float | None]] = [
            (exclude, max_dur), (exclude, None), ([], max_dur), ([], None),
        ]
        seen: set[tuple[tuple[str, ...], float | None]] = set()
        for tags, limit in attempts:
            key = (tuple(tags), limit)
            if key in seen:
                continue
            seen.add(key)
            seg = self.db.pick_ready("music", now=now, exclude_tags=tags, max_duration_s=limit)
            if seg is not None:
                return seg
        return None

    def _last_music_artist_tags(self, plays: Sequence[PlayLogEntry]) -> list[str]:
        """Etiquetas artist:* de la última canción emitida dentro de la ventana."""
        for play in reversed(plays):
            if play.kind != "music":
                continue
            seg = self.db.get_segment(play.segment_id) if play.segment_id else None
            if seg is None:
                return []
            return [t for t in seg.tags if t.startswith(ARTIST_PREFIX)]
        return []

    def _music_deadline_s(self, now: datetime) -> float | None:
        """
        Segundos máximos que puede durar una canción para no pisar la próxima señal
        horaria (debe terminar antes del minuto ``TIME_SIGNAL_WINDOW_MIN``).
        None si la señal está desactivada o no hay señal preparada para esa hora.
        """
        if not self.scheduler.grid.time_signal_enabled:
            return None
        local = now.astimezone(self.tz)
        # Aritmética en UTC para respetar los cambios de horario
        top = local.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        next_hour = (top + timedelta(hours=1)).astimezone(self.tz)
        if self._ready_time_signal(next_hour, now) is None:
            return None
        deadline = next_hour + timedelta(minutes=TIME_SIGNAL_WINDOW_MIN)
        return (deadline - now).total_seconds() - _DEADLINE_MARGIN_S

    # ── Emisión ──────────────────────────────────────────────────────────────

    def _air(self, seg: Segment, kind: SegmentKind, reason: str) -> PlayOutcome:
        started = self._now()
        play_id = self.db.log_play_start(seg.id, kind, self.mode, started)
        outcome = PlayOutcome(
            segment_id=seg.id,
            kind=kind,
            title=seg.title,
            started_at=started,
            duration_s=seg.duration_s,
            reason=reason,
        )
        logger.info(
            "En antena %s [%s] %s (%.0f s) — %s",
            started.astimezone(self.tz).strftime("%H:%M:%S"),
            kind, outcome.title, outcome.duration_s, reason,
        )
        self._interrupted = False
        try:
            self.audio.play(seg.path)
        except Exception:
            logger.exception("Error reproduciendo %s", seg.path)
            self._interrupted = True
        interrupted = self._interrupted
        self.db.log_play_end(play_id, self._now(), skipped=interrupted)
        # Un segmento de palabra interrumpido sigue "ready" para poder emitirse después
        if kind not in REUSABLE_KINDS and not interrupted:
            self.db.update_segment_status(seg.id, "retired")
        return outcome

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
