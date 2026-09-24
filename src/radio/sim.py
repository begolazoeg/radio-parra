"""
Simulación acelerada de la emisora (``radio simulate``).

Monta la emisora completa en memoria —BD ``:memory:``, ``FakeClock``, catálogo
musical sintético y el producer de señal horaria con ``FakeLLM``/``FakeTTS``— y la
hace funcionar N horas de tiempo simulado sin esperar ni reproducir audio.

Cómo avanza el tiempo
---------------------
``SimAudioBackend`` sustituye al reproductor: en ``play(path)`` avanza el
``FakeClock`` la duración del segmento, que obtiene de la BD por su ``path``.
Así el ``Playout`` real registra ``started_at``/``ended_at`` correctos sin saber
que está en una simulación. Si un paso no emite nada, el bucle avanza el reloj
``DEAD_AIR_STEP_S`` segundos y lo cuenta como silencio.

Determinismo: todo el azar sale de ``random.Random(seed)`` (catálogo y scheduler) y
el informe no incluye ids ni rutas; misma semilla → informe idéntico.

Invariantes duros (``SimReport.failures``): sin silencio, sin artista repetido en
canciones consecutivas, al menos ``horas - 1`` señales horarias y ningún producer
con error.
"""

from __future__ import annotations

import json
import random
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from radio.core.clock import FakeClock
from radio.core.config import ProducersConfig, ProducerSettings, RadioConfig
from radio.core.models import Segment
from radio.core.playout import ARTIST_PREFIX, Playout, PlayOutcome
from radio.core.scheduler import ALL_KINDS, BUDGET_WINDOW, TALK_KINDS, Scheduler
from radio.core.store import DB
from radio.producers import ProducerContext, ProducerRunner, TimeSignalProducer
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.fake import FakeTTS

# ── Parámetros de la simulación ───────────────────────────────────────────────

SIM_START = datetime(2026, 1, 5, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
N_TRACKS = 250
N_ARTISTS = 40
TRACK_DURATION_S = (150.0, 600.0)
N_JINGLES = 3
JINGLE_DURATION_S = (6.0, 10.0)
DEAD_AIR_STEP_S = 5.0
DECISIONS_SAMPLE = 12

# Producers activos en la simulación (no se toca producers.yaml)
SIM_PRODUCERS: dict[str, ProducerSettings] = {
    "time_signal": ProducerSettings(active=True, target_stock=2, cron="*/30 * * * *"),
}


# ── Backend de audio simulado ─────────────────────────────────────────────────

class SimAudioBackend:
    """Backend que no suena: avanza el FakeClock la duración de cada audio."""

    def __init__(self, clock: FakeClock, duration_of: Callable[[Path], float]) -> None:
        self.clock = clock
        self.duration_of = duration_of
        self.played: list[Path] = []

    def play(self, path: Path) -> None:
        self.played.append(path)
        self.clock.advance(self.duration_of(path))

    def enqueue(self, path: Path) -> None:
        self.play(path)

    def skip(self) -> None:
        """Nada que cortar: la reproducción simulada es instantánea."""


# ── Informe ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    """Una decisión de emisión (para la muestra del informe)."""
    at: str          # hora local HH:MM:SS
    kind: str
    title: str
    reason: str


@dataclass
class SimReport:
    """Resultado de una simulación (texto legible o JSON)."""
    seed: int
    hours: float
    start: str
    end: str
    segments_aired: int
    airtime_s: dict[str, float]
    airtime_pct: dict[str, float]
    music_share: float
    max_talk_ratio_rolling_hour: float
    talk_budget_ratio: float
    time_signals_aired: int
    time_signals_on_time: int          # emitidas en los minutos 0–4 de la hora
    time_signals_expected_min: int     # horas - 1 (la primera hora no tiene señal previa)
    back_to_back_artist: int
    dead_air_s: float
    producer_runs: int
    producer_errors: int
    decisions_sample: list[Decision] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_text(self) -> str:
        lines = [
            f"Simulación Radio Parra — {self.hours:g} h, semilla {self.seed}",
            f"  Desde {self.start} hasta {self.end}",
            f"  Segmentos emitidos: {self.segments_aired}",
            "",
            "Tiempo de antena por tipo:",
        ]
        for kind, secs in self.airtime_s.items():
            lines.append(f"  {kind:<12} {secs:>9.0f} s  {self.airtime_pct[kind]:>5.1f} %")
        lines += [
            "",
            f"Música: {self.music_share * 100:.1f} % del tiempo de antena",
            f"Palabra máx. en hora móvil: {self.max_talk_ratio_rolling_hour * 100:.1f} % "
            f"(presupuesto {self.talk_budget_ratio * 100:.0f} %)",
            f"Señales horarias: {self.time_signals_aired} emitidas "
            f"({self.time_signals_on_time} en minuto 0–4; mínimo esperado "
            f"{self.time_signals_expected_min})",
            f"Mismo artista seguido: {self.back_to_back_artist}",
            f"Silencio: {self.dead_air_s:.0f} s",
            f"Producers: {self.producer_runs} ejecuciones, {self.producer_errors} con error",
            "",
            "Muestra de decisiones del scheduler:",
        ]
        for d in self.decisions_sample:
            lines.append(f"  {d.at} {d.kind:<11} {d.title} — {d.reason}")
        lines.append("")
        if self.passed:
            lines.append("RESULTADO: OK (todos los invariantes se cumplen)")
        else:
            lines.append("RESULTADO: FALLO")
            lines += [f"  - {f}" for f in self.failures]
        return "\n".join(lines)


# ── Catálogo sintético ────────────────────────────────────────────────────────

def build_catalog(db: DB, rng: random.Random, created_at: datetime) -> None:
    """
    Inserta N_TRACKS canciones de N_ARTISTS artistas y N_JINGLES jingles, con rutas
    ficticias. El orden de alta se baraja para que la rotación mezcle artistas.
    """
    artists = [f"artista-{i:02d}" for i in range(N_ARTISTS)]
    tracks = [
        (f"sim-music-{i:03d}", artists[i % N_ARTISTS], rng.uniform(*TRACK_DURATION_S))
        for i in range(N_TRACKS)
    ]
    rng.shuffle(tracks)
    for order, (seg_id, artist, duration) in enumerate(tracks):
        db.add_segment(
            Segment(
                id=seg_id,
                kind="music",
                factual=False,
                path=Path(f"/sim/music/{seg_id}.mp3"),
                duration_s=round(duration, 3),
                # created_at distinto por pista: fija el orden de la primera rotación
                created_at=created_at + timedelta(microseconds=order),
                producer="sim",
                meta={
                    "title": f"{artist} — tema {seg_id[-3:]}",
                    "tags": [f"{ARTIST_PREFIX}{artist}", "source:tiny_desk"],
                },
            )
        )
    for i in range(N_JINGLES):
        db.add_segment(
            Segment(
                id=f"sim-jingle-{i}",
                kind="jingle",
                factual=False,
                path=Path(f"/sim/jingles/jingle-{i}.wav"),
                duration_s=round(rng.uniform(*JINGLE_DURATION_S), 3),
                created_at=created_at,
                producer="sim",
                meta={"title": f"Jingle {i + 1}"},
            )
        )


# ── Métricas ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Aired:
    kind: str
    start: datetime
    end: datetime
    segment_id: str


def _max_rolling_talk_ratio(aired: Sequence[_Aired], origin: datetime) -> float:
    """Máxima proporción de palabra en ventanas de 60 min que acaban al final de cada segmento."""
    best = 0.0
    for item in aired:
        window_end = item.end
        window_start = window_end - BUDGET_WINDOW
        if window_start < origin:
            continue
        talk = total = 0.0
        for rec in aired:
            clipped = (min(rec.end, window_end) - max(rec.start, window_start)).total_seconds()
            if clipped <= 0:
                continue
            total += clipped
            if rec.kind in TALK_KINDS:
                talk += clipped
        if total > 0:
            best = max(best, talk / total)
    return best


def _back_to_back_artists(db: DB, aired: Sequence[_Aired]) -> int:
    """Canciones consecutivas (ignorando lo que suene entre medias) del mismo artista."""
    count = 0
    previous: set[str] | None = None
    for item in aired:
        if item.kind != "music":
            continue
        seg = db.get_segment(item.segment_id)
        tags = {t for t in (seg.tags if seg else ()) if t.startswith(ARTIST_PREFIX)}
        if previous is not None and tags and tags & previous:
            count += 1
        previous = tags
    return count


# ── Simulación ────────────────────────────────────────────────────────────────

def sim_config(config: RadioConfig) -> RadioConfig:
    """Copia de la configuración con los producers de la simulación activados."""
    return config.model_copy(update={"producers": ProducersConfig(producers=dict(SIM_PRODUCERS))})


def run_simulation(
    *,
    hours: float = 24.0,
    seed: int = 1,
    config: RadioConfig,
    prompts_dir: Path = Path("prompts"),
    start: datetime = SIM_START,
) -> SimReport:
    """Ejecuta la simulación y devuelve el informe (no lanza por invariantes)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo(config.station.timezone))
    config = sim_config(config)
    tz = ZoneInfo(config.station.timezone)

    db = DB(":memory:")
    clock = FakeClock(start)
    build_catalog(db, random.Random(seed), start)
    scheduler = Scheduler(config.grid, rng=random.Random(seed))

    def duration_of(path: Path) -> float:
        seg = db.find_by_path(path)
        return seg.duration_s if seg else 0.0

    end = start + timedelta(hours=hours)
    outcomes: list[PlayOutcome] = []
    dead_air = 0.0
    try:
        with tempfile.TemporaryDirectory(prefix="radio-sim-") as tmp:
            ctx = ProducerContext(
                db=db,
                clock=clock,
                llm=FakeLLM(),
                tts=FakeTTS(),
                config=config,
                data_dir=Path(tmp),
                prompts_dir=prompts_dir,
            )
            runner = ProducerRunner(ctx, [TimeSignalProducer()])
            playout = Playout(
                db,
                scheduler,
                SimAudioBackend(clock, duration_of),
                clock,
                tz=config.station.timezone,
                verify_files=False,
            )
            while clock.now() < end:
                runner.tick()
                outcome = playout.step()
                if outcome is None:
                    clock.advance(DEAD_AIR_STEP_S)
                    dead_air += DEAD_AIR_STEP_S
                else:
                    outcomes.append(outcome)

        return _build_report(
            db, config, tz, seed=seed, hours=hours, start=start, end=clock.now(),
            outcomes=outcomes, dead_air=dead_air,
        )
    finally:
        db.close()


def _build_report(
    db: DB,
    config: RadioConfig,
    tz: ZoneInfo,
    *,
    seed: int,
    hours: float,
    start: datetime,
    end: datetime,
    outcomes: Sequence[PlayOutcome],
    dead_air: float,
) -> SimReport:
    aired = [
        _Aired(kind=p.kind, start=p.started_at, end=p.ended_at, segment_id=p.segment_id)
        for p in db.list_play_log()
        if p.ended_at is not None and p.segment_id is not None
    ]
    airtime: dict[str, float] = {k: 0.0 for k in ALL_KINDS}
    for item in aired:
        airtime[item.kind] = airtime.get(item.kind, 0.0) + (item.end - item.start).total_seconds()
    total = sum(airtime.values()) or 1.0
    airtime_pct = {k: round(v / total * 100, 2) for k, v in airtime.items()}

    signals = [a for a in aired if a.kind == "time_signal"]
    on_time = sum(1 for a in signals if a.start.astimezone(tz).minute < 5)
    expected = max(0, int(hours) - 1)
    back_to_back = _back_to_back_artists(db, aired)
    runs = db.list_producer_runs()
    errors = sum(1 for r in runs if r.ok is False)

    failures: list[str] = []
    if dead_air > 0:
        failures.append(f"silencio en antena: {dead_air:.0f} s")
    if back_to_back > 0:
        failures.append(f"mismo artista en canciones consecutivas: {back_to_back}")
    if len(signals) < expected:
        failures.append(f"señales horarias insuficientes: {len(signals)} < {expected}")
    if errors > 0:
        failures.append(f"producers con error: {errors}")

    sample = [
        Decision(
            at=o.started_at.astimezone(tz).strftime("%H:%M:%S"),
            kind=o.kind,
            title=o.title,
            reason=o.reason,
        )
        for o in outcomes[:DECISIONS_SAMPLE]
    ]
    return SimReport(
        seed=seed,
        hours=hours,
        start=start.isoformat(),
        end=end.isoformat(),
        segments_aired=len(aired),
        airtime_s={k: round(v, 2) for k, v in airtime.items()},
        airtime_pct=airtime_pct,
        music_share=round(airtime["music"] / total, 4),
        max_talk_ratio_rolling_hour=round(_max_rolling_talk_ratio(aired, start), 4),
        talk_budget_ratio=config.grid.talk_budget_ratio,
        time_signals_aired=len(signals),
        time_signals_on_time=on_time,
        time_signals_expected_min=expected,
        back_to_back_artist=back_to_back,
        dead_air_s=dead_air,
        producer_runs=len(runs),
        producer_errors=errors,
        decisions_sample=sample,
        failures=failures,
    )
