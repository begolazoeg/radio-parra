"""
Simulación acelerada de la emisora (``radio simulate``, §9).

Es la herramienta principal para afinar ``grid.yaml``, así que ejecuta **el mismo
motor que la emisora real** (``radio.station.StationEngine``): mismo lookahead,
mismas interrupciones, mismo ``play_log``, misma escalera de degradación. Solo
cambian los adaptadores:

- ``FakeClock`` en lugar del reloj del sistema;
- ``FakeEventBackend(advance=clock.advance)`` en lugar de mpv: cada archivo que
  termina avanza el reloj lo que le queda de su duración (la de su segmento en la BD);
- BD ``:memory:`` con un catálogo musical sintético.

Producción simulada
-------------------
La emisora no produce (invariante 2), así que la simulación modela aparte el timer de
``deploy/radio-produce.timer``: cada ``PRODUCE_EVERY`` de tiempo simulado se ejecuta
``producers.runner.produce`` (lo mismo que ``radio produce --all``) con los productores
de ``SIM_PRODUCERS``: la señal horaria y el locutor (``host_intro``, Fase 2) con el
pipeline real (gather → write → grounding → tts → register) pero con dobles: fuentes
sintéticas y un ``FakeLLM`` que responde pegado a ellas
(``radio.producers.host_intro_fake``), y un TTS falso que declara una duración
proporcional al texto sin escribir audio de verdad (``SimTTS``). Así aparecen las
unidades vinculadas ``[host_intro, music]`` y cuentan para el presupuesto de charla.
La música no se descarga: es stock sintético (``--catalog``):

- ``default``: 250 canciones de 40 artistas, 150–600 s, y 3 jingles;
- ``tinydesk``: 40 conciertos de artistas distintos, 900–1800 s, como el feed de Tiny
  Desk (y los mismos jingles).

Con ``talk_stock=True`` se añade además stock sintético de palabra (un lote por cada
kind de los ``talk_pool`` de la parrilla, factual o ficción según
``FACTUAL_TALK_KINDS``) e intros ``host_intro`` vinculadas a parte de las canciones.

Bucle de eventos
----------------
``drive`` avanza el reloj al siguiente de estos instantes: fin del archivo en curso
(``backend.finish()``), temporizador del motor (``engine.tick()``: interrupciones,
reintentos), pasada de producción o fin de la simulación. Si no suena nada mientras
el reloj avanza, se cuenta como silencio.

Determinismo: todo el azar sale de ``random.Random(seed)`` (catálogo y scheduler) y el
informe no incluye ids ni rutas; misma semilla → informe idéntico.

Invariantes duros (``SimReport.failures``): sin silencio, sin artista repetido en
canciones consecutivas, al menos ``horas - 1`` señales horarias (todas dentro de su
``max_late_seconds``), la charla nunca por encima de ``talk_budget.max_ratio`` en
ninguna ventana móvil, nunca ficción justo después de factual, ninguna intro sin su
canción justo detrás, ningún peldaño 5 y ningún producer con error.
"""

from __future__ import annotations

import json
import random
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from radio.core.clock import FakeClock
from radio.core.config import GridConfig, ProducersConfig, ProducerSettings, RadioConfig
from radio.core.models import AudioInfo, Segment, Voice
from radio.core.store import DB
from radio.grid.budget import is_talk
from radio.grid.rules import resolve_mode
from radio.grid.scheduler import ARTIST_PREFIX, RUNG_EMERGENCY, SEPARATOR_KINDS
from radio.producers.base import ProducerContext
from radio.producers.host_intro import HostIntroProducer
from radio.producers.host_intro_fake import fake_intro_llm, fake_sources
from radio.producers.runner import produce
from radio.providers.audio.fake import FakeEventBackend
from radio.providers.tts.fake import FakeTTS
from radio.station.engine import AiredItem, StationEngine, audio_duration

# ── Parámetros de la simulación ───────────────────────────────────────────────

SIM_START = datetime(2026, 1, 5, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))

Catalog = Literal["default", "tinydesk"]
CATALOGS: tuple[str, ...] = ("default", "tinydesk")

# Catálogo "default"
N_TRACKS = 250
N_ARTISTS = 40
TRACK_DURATION_S = (150.0, 600.0)
# Catálogo "tinydesk": un concierto por artista, como el feed
N_CONCERTS = 40
CONCERT_DURATION_S = (900.0, 1800.0)

N_JINGLES = 3
JINGLE_DURATION_S = (6.0, 10.0)
DECISIONS_SAMPLE = 12

# Cada cuánto corre la producción simulada (deploy/radio-produce.timer: OnCalendar=*:0/15)
PRODUCE_EVERY = timedelta(minutes=15)

# Stock sintético de palabra (``talk_stock=True``)
N_TALK_PER_KIND = 40
TALK_DURATION_S = (45.0, 90.0)
INTRO_EVERY_N_TRACKS = 3
INTRO_DURATION_S = (10.0, 25.0)
FACTUAL_TALK_KINDS: frozenset[str] = frozenset({
    "weather", "ephemeris", "sky", "word_of_day", "artist_fact", "news", "agenda",
    "birthday", "dedication", "voicemail", "serial_classic", "parra_report", "trivia",
})

# Kinds que siempre aparecen en el informe de tiempo de antena
REPORT_KINDS: tuple[str, ...] = ("music", "host_intro", "jingle", "time_signal")

# Productores de la simulación (no se toca producers.yaml: music_tinydesk necesita red)
SIM_PRODUCERS: dict[str, ProducerSettings] = {
    "time_signal": ProducerSettings(active=True, target_stock=2, cron="*/30 * * * *"),
    "host_intro": ProducerSettings(active=True, target_stock=10, cron="15 */2 * * *"),
}


class SimTTS(FakeTTS):
    """
    TTS de la simulación: misma duración que ``FakeTTS`` (proporcional al texto) pero
    escribe un WAV mínimo, para no generar cientos de MB de silencio en 48 h.
    """

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        info = super().synthesize(text[:1], voice, out_path)
        return AudioInfo(path=info.path, duration_s=max(0.1, len(text) / self.chars_per_second))


# ── Informe ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    """Una decisión de emisión (para la muestra del informe)."""
    at: str          # hora local HH:MM:SS
    kind: str
    title: str
    reason: str
    rung: int = 1


@dataclass(frozen=True)
class TimelineEntry:
    """Una línea de la línea de tiempo: un archivo que sonó."""
    at: str          # hora local "DD HH:MM:SS"
    kind: str
    title: str
    duration_s: float
    rung: int
    flags: str = ""  # "interrupción", "cortado", "error"...

    def to_text(self) -> str:
        flags = f"  [{self.flags}]" if self.flags else ""
        return (f"{self.at}  {self.rung}  {self.kind:<11} {self.duration_s:>7.1f} s  "
                f"{self.title}{flags}")


@dataclass
class SimReport:
    """Resultado de una simulación (texto legible o JSON)."""
    seed: int
    hours: float
    mode: str
    catalog: str
    start: str
    end: str
    segments_aired: int
    units_aired: int
    linked_units: int                  # unidades [host_intro, music] emitidas
    airtime_s: dict[str, float]
    airtime_pct: dict[str, float]
    music_share: float
    max_talk_ratio_rolling_hour: float
    talk_budget_ratio: float
    time_signals_aired: int
    time_signals_on_time: int          # dentro de max_late_seconds tras la hora en punto
    time_signals_expected_min: int     # horas - 1 (la primera hora no tiene señal previa)
    interrupts: int                    # veces que una interrupción se adelantó a la cola
    music_cuts: int                    # canciones cortadas para dar paso a una interrupción
    back_to_back_artist: int
    fiction_after_factual: int
    orphan_intros: int                 # intros emitidas sin su canción justo detrás
    rung_histogram: dict[str, int]     # peldaño de la escalera (§8) → unidades
    dead_air_s: float
    producer_runs: int
    producer_errors: int
    # Normalización en reproducción: archivos medidos y ganancia aplicada (dB)
    gain_db: dict[str, float | int | None] = field(default_factory=dict)
    decisions_sample: list[Decision] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self, *, timeline: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not timeline:
            del data["timeline"]
        data["passed"] = self.passed
        return data

    def to_json(self, *, timeline: bool = False) -> str:
        return json.dumps(self.to_dict(timeline=timeline), ensure_ascii=False, indent=2)

    def timeline_text(self) -> str:
        header = "DD HH:MM:SS  P  kind          durac.    título"
        return "\n".join([header, *(t.to_text() for t in self.timeline)])

    def _gain_text(self) -> str:
        g = self.gain_db
        if not g.get("measured"):
            return "Ganancia en reproducción: ningún archivo con medida de loudness"
        return (f"Ganancia en reproducción ({g['measured']} archivos medidos): "
                f"mín {g['min']:+.1f} dB, máx {g['max']:+.1f} dB, media {g['mean']:+.1f} dB")

    def to_text(self, *, timeline: bool = False) -> str:
        lines = [
            f"Simulación Radio Parra — {self.hours:g} h, semilla {self.seed}, modo {self.mode}, "
            f"catálogo {self.catalog}",
            f"  Desde {self.start} hasta {self.end}",
            f"  Unidades emitidas: {self.units_aired} ({self.segments_aired} segmentos; "
            f"{self.linked_units} con intro del locutor)",
            "",
        ]
        if timeline:
            lines += ["Línea de tiempo (P = peldaño de la escalera, §8):", self.timeline_text(), ""]
        lines.append("Tiempo de antena por tipo:")
        for kind, secs in self.airtime_s.items():
            lines.append(f"  {kind:<12} {secs:>9.0f} s  {self.airtime_pct[kind]:>5.1f} %")
        lines += [
            "",
            f"Música: {self.music_share * 100:.1f} % del tiempo de antena",
            f"Palabra máx. en ventana móvil: {self.max_talk_ratio_rolling_hour * 100:.1f} % "
            f"(tope {self.talk_budget_ratio * 100:.0f} %)",
            f"Señales horarias: {self.time_signals_aired} emitidas "
            f"({self.time_signals_on_time} a tiempo; mínimo esperado "
            f"{self.time_signals_expected_min})",
            f"Interrupciones adelantadas a la cola: {self.interrupts} "
            f"(canciones cortadas: {self.music_cuts})",
            f"Mismo artista seguido: {self.back_to_back_artist}",
            f"Ficción justo después de factual: {self.fiction_after_factual}",
            f"Intros sin su canción detrás: {self.orphan_intros}",
            "Escalera de degradación (peldaño: unidades): "
            + ", ".join(f"{k}: {v}" for k, v in self.rung_histogram.items()),
            f"Silencio: {self.dead_air_s:.0f} s",
            f"Producers: {self.producer_runs} ejecuciones, {self.producer_errors} con error",
            self._gain_text(),
            "",
            "Muestra de decisiones del scheduler:",
        ]
        for d in self.decisions_sample:
            lines.append(f"  {d.at} [{d.rung}] {d.kind:<11} {d.title} — {d.reason}")
        lines.append("")
        if self.passed:
            lines.append("RESULTADO: OK (todos los invariantes se cumplen)")
        else:
            lines.append("RESULTADO: FALLO")
            lines += [f"  - {f}" for f in self.failures]
        return "\n".join(lines)


# ── Catálogo sintético ────────────────────────────────────────────────────────

def build_catalog(
    db: DB, rng: random.Random, created_at: datetime, catalog: Catalog = "default"
) -> None:
    """
    Inserta la música del catálogo y N_JINGLES jingles, con rutas ficticias. El orden
    de alta se baraja para que la rotación mezcle artistas.
    """
    if catalog == "tinydesk":
        tracks = [
            (f"sim-music-{i:03d}", f"artista-{i:02d}", rng.uniform(*CONCERT_DURATION_S))
            for i in range(N_CONCERTS)
        ]
    else:
        artists = [f"artista-{i:02d}" for i in range(N_ARTISTS)]
        tracks = [
            (f"sim-music-{i:03d}", artists[i % N_ARTISTS], rng.uniform(*TRACK_DURATION_S))
            for i in range(N_TRACKS)
        ]
    rng.shuffle(tracks)
    for order, (seg_id, artist, duration) in enumerate(tracks):
        title = (f"Tiny Desk: {artist}" if catalog == "tinydesk"
                 else f"{artist} — tema {seg_id[-3:]}")
        db.add_segment(
            Segment(
                id=seg_id,
                kind="music",
                factual=False,
                path=Path(f"/sim/music/{seg_id}.mp3"),
                duration_s=round(duration, 3),
                # created_at distinto por pista: orden estable
                created_at=created_at + timedelta(microseconds=order),
                producer="sim",
                meta={"title": title, "artist": artist,
                      "tags": [f"{ARTIST_PREFIX}{artist}", "source:tiny_desk"],
                      **_sim_loudness(seg_id)},
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


def _sim_loudness(seg_id: str) -> dict[str, float]:
    """
    Medida de loudness sintética (como la que guarda ``music_tinydesk``). Usa su propio
    generador por pista para no alterar la secuencia del catálogo ni la simulación.
    """
    rng = random.Random(f"loudness:{seg_id}")
    lufs = rng.uniform(-24.0, -12.0)
    return {"loudness_lufs": round(lufs, 2),
            "true_peak_db": round(min(0.0, lufs + rng.uniform(9.0, 17.0)), 2)}


def talk_kinds(grid: GridConfig) -> list[str]:
    """Kinds de palabra que aparecen en algún ``talk_pool`` (orden estable)."""
    return sorted({
        kind
        for mode in grid.modes.values()
        for part in mode.dayparts
        for kind, weight in part.talk_pool.items()
        if weight > 0
    })


def build_talk_stock(db: DB, rng: random.Random, created_at: datetime, grid: GridConfig) -> None:
    """Stock sintético de palabra e intros vinculadas (ver docstring del módulo)."""
    for kind in talk_kinds(grid):
        for i in range(N_TALK_PER_KIND):
            seg_id = f"sim-{kind}-{i:03d}"
            db.add_segment(Segment(
                id=seg_id,
                kind=kind,
                factual=kind in FACTUAL_TALK_KINDS,
                path=Path(f"/sim/{kind}/{seg_id}.mp3"),
                duration_s=round(rng.uniform(*TALK_DURATION_S), 3),
                created_at=created_at + timedelta(microseconds=i),
                producer="sim",
                meta={"title": f"{kind} {i + 1}"},
            ))
    music = sorted(db.list_segments(kind="music"), key=lambda s: s.id)
    for i, track in enumerate(music[::INTRO_EVERY_N_TRACKS]):
        seg_id = f"sim-intro-{i:03d}"
        db.add_segment(Segment(
            id=seg_id,
            kind="host_intro",
            factual=True,
            path=Path(f"/sim/host_intro/{seg_id}.mp3"),
            duration_s=round(rng.uniform(*INTRO_DURATION_S), 3),
            created_at=created_at,
            producer="sim",
            parent_id=track.id,
            meta={"title": f"Intro de {track.title}"},
        ))


# ── Bucle de eventos ──────────────────────────────────────────────────────────

@dataclass
class Job:
    """Tarea periódica en tiempo simulado (p. ej. el timer de producción)."""
    every: timedelta
    run: Callable[[], object]
    next_at: datetime | None = None


def drive(
    engine: StationEngine,
    backend: FakeEventBackend,
    clock: FakeClock,
    end: datetime,
    *,
    jobs: Sequence[Job] = (),
) -> float:
    """
    Conduce ``engine`` (ya arrancado) hasta ``end`` en tiempo simulado, sin hilos.
    Devuelve los segundos de silencio (reloj avanzando sin nada sonando).

    En cada vuelta salta al instante más próximo entre: una tarea de ``jobs`` (primero,
    para que el stock esté al día), el fin del archivo en curso, ``engine.next_wakeup()``
    y ``end``.
    """
    dead_air = 0.0
    for job in jobs:
        if job.next_at is None:
            job.next_at = clock.now() + job.every
    while clock.now() < end:
        now = clock.now()
        left = backend.time_left()
        t_end = None if left is None else now + timedelta(seconds=left)
        t_wake = engine.next_wakeup()
        due_job = min(jobs, key=lambda j: j.next_at or end, default=None)
        t_job = due_job.next_at if due_job is not None else None
        target = min(t for t in (t_job, t_end, t_wake, end) if t is not None)
        target = max(target, now)
        if backend.current() is None:
            dead_air += (target - now).total_seconds()
        if due_job is not None and t_job == target:
            clock.advance((target - now).total_seconds())
            due_job.run()
            due_job.next_at = target + due_job.every
        elif t_end is not None and t_end == target:
            backend.finish()           # avanza el reloj lo que le queda al archivo
        elif t_wake is not None and t_wake == target:
            clock.advance((target - now).total_seconds())
            engine.tick()
        else:
            clock.advance((target - now).total_seconds())
    return dead_air


# ── Métricas ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Aired:
    kind: str
    start: datetime
    end: datetime
    segment_id: str | None


def _max_rolling_talk_ratio(
    aired: Sequence[_Aired], origin: datetime, window: timedelta
) -> float:
    """Máxima proporción de palabra en ventanas que acaban al final de cada segmento."""
    best = 0.0
    for item in aired:
        window_end = item.end
        window_start = window_end - window
        if window_start < origin:
            continue
        talk = total = 0.0
        for rec in aired:
            clipped = (min(rec.end, window_end) - max(rec.start, window_start)).total_seconds()
            if clipped <= 0:
                continue
            total += clipped
            if is_talk(rec.kind):
                talk += clipped
        if total > 0:
            best = max(best, talk / total)
    return best


def _back_to_back_artists(db: DB, aired: Sequence[_Aired]) -> int:
    """Canciones consecutivas (ignorando lo que suene entre medias) del mismo artista."""
    count = 0
    previous: set[str] | None = None
    for item in aired:
        if item.kind != "music" or item.segment_id is None:
            continue
        seg = db.get_segment(item.segment_id)
        tags = {t for t in (seg.tags if seg else ()) if t.startswith(ARTIST_PREFIX)}
        if previous is not None and tags and tags & previous:
            count += 1
        previous = tags
    return count


def _fiction_after_factual(db: DB, aired: Sequence[_Aired]) -> int:
    """Segmentos de ficción emitidos justo después de uno factual (§1.4)."""
    count = 0
    prev_factual = False
    for item in aired:
        seg = db.get_segment(item.segment_id) if item.segment_id else None
        if item.kind in SEPARATOR_KINDS or seg is None:
            prev_factual = False
            continue
        if prev_factual and not seg.factual:
            count += 1
        prev_factual = seg.factual
    return count


def _linked_units(db: DB, aired: Sequence[_Aired]) -> tuple[int, int]:
    """(unidades ``[host_intro, music]``, intros sin su canción justo detrás)."""
    linked = orphans = 0
    for n, item in enumerate(aired):
        if item.kind != "host_intro":
            continue
        intro = db.get_segment(item.segment_id) if item.segment_id else None
        nxt = aired[n + 1] if n + 1 < len(aired) else None
        if intro is not None and nxt is not None and nxt.segment_id == intro.parent_id:
            linked += 1
        elif nxt is not None:           # la última puede quedar cortada por el fin
            orphans += 1
    return linked, orphans


def _signal_delay_s(start: datetime, tz: ZoneInfo) -> float:
    """Segundos desde la hora en punto (local) hasta ``start``."""
    local = start.astimezone(tz)
    top = local.replace(minute=0, second=0, microsecond=0)
    return (start.astimezone(UTC) - top.astimezone(UTC)).total_seconds()


# ── Simulación ────────────────────────────────────────────────────────────────

def sim_config(config: RadioConfig) -> RadioConfig:
    """
    Copia de la configuración con los productores de la simulación (los parámetros de
    ``host_intro`` se toman de producers.yaml si están).
    """
    producers = {k: v.model_copy(deep=True) for k, v in SIM_PRODUCERS.items()}
    real = config.producers.get("host_intro")
    if real is not None:
        producers["host_intro"].params = dict(real.params)
    return config.model_copy(update={"producers": ProducersConfig(producers=producers)})


def run_simulation(
    *,
    hours: float = 24.0,
    seed: int = 1,
    config: RadioConfig,
    prompts_dir: Path = Path("prompts"),
    start: datetime = SIM_START,
    mode: str = "default",
    talk_stock: bool = False,
    catalog: Catalog = "default",
) -> SimReport:
    """Ejecuta la simulación y devuelve el informe (no lanza por invariantes)."""
    if catalog not in CATALOGS:
        raise ValueError(f"catálogo desconocido {catalog!r} (disponibles: {', '.join(CATALOGS)})")
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo(config.station.timezone))
    config = sim_config(config)
    tz = ZoneInfo(config.station.timezone)

    db = DB(":memory:")
    # El reloj avanza en UTC: sumar segundos a una hora local es aritmética de pared
    # y se descuadra en los cambios de hora
    clock = FakeClock(start.astimezone(UTC))
    catalog_rng = random.Random(seed)
    build_catalog(db, catalog_rng, start, catalog)
    if talk_stock:
        build_talk_stock(db, catalog_rng, start, config.grid)

    durations: dict[Path, float] = {}

    def duration_of(path: Path) -> float:
        if path not in durations:
            seg = db.find_by_path(path)
            durations[path] = seg.duration_s if seg else audio_duration(path)
        return durations[path]

    end = start.astimezone(UTC) + timedelta(hours=hours)
    aired: list[AiredItem] = []
    try:
        with tempfile.TemporaryDirectory(prefix="radio-sim-") as tmp:
            ctx = ProducerContext(
                db=db, clock=clock, llm=fake_intro_llm(), tts=SimTTS(), config=config,
                data_dir=Path(tmp), prompts_dir=prompts_dir,
            )
            sim_producers = {
                "host_intro": HostIntroProducer(config, source_gatherer=fake_sources),
            }
            backend = FakeEventBackend(clock, duration_of=duration_of, advance=clock.advance)
            engine = StationEngine.from_config(
                config, db, backend, clock, mode=mode, rng=random.Random(seed),
                verify_files=False, on_aired=aired.append,
            )
            produce(ctx, producers=sim_producers)     # stock inicial (el timer ya corrió)
            engine.start()
            dead_air = drive(engine, backend, clock, end, jobs=[
                Job(PRODUCE_EVERY, lambda: produce(ctx, producers=sim_producers)),
            ])
            engine.stop()
            backend.close()
        return _build_report(
            db, config, tz, engine, aired, seed=seed, hours=hours, mode=mode, catalog=catalog,
            start=start, end=clock.now().astimezone(tz), dead_air=dead_air,
        )
    finally:
        db.close()


def _build_report(
    db: DB,
    config: RadioConfig,
    tz: ZoneInfo,
    engine: StationEngine,
    items: Sequence[AiredItem],
    *,
    seed: int,
    hours: float,
    mode: str,
    catalog: str,
    start: datetime,
    end: datetime,
    dead_air: float,
) -> SimReport:
    grid = config.grid
    aired = [
        _Aired(kind=p.kind, start=p.started_at, end=p.ended_at, segment_id=p.segment_id)
        for p in db.list_play_log()
        if p.ended_at is not None
    ]
    airtime: dict[str, float] = {k: 0.0 for k in REPORT_KINDS}
    for item in aired:
        airtime[item.kind] = airtime.get(item.kind, 0.0) + (item.end - item.start).total_seconds()
    total = sum(airtime.values()) or 1.0
    airtime_pct = {k: round(v / total * 100, 2) for k, v in airtime.items()}

    _, mode_cfg = resolve_mode(grid, mode)
    signal_rules = [r for r in mode_cfg.interrupts if r.kind == "time_signal"]
    max_late = max((r.max_late_seconds for r in signal_rules), default=0.0)
    signals = [a for a in aired if a.kind == "time_signal"]
    on_time = sum(1 for a in signals if _signal_delay_s(a.start, tz) <= max_late)
    expected = max(0, int(hours) - 1) if signal_rules else 0
    back_to_back = _back_to_back_artists(db, aired)
    fiction_after = _fiction_after_factual(db, aired)
    linked, orphans = _linked_units(db, aired)
    window = timedelta(minutes=grid.talk_budget.window_minutes)
    max_ratio = _max_rolling_talk_ratio(aired, start, window)
    runs = db.list_producer_runs()
    errors = sum(1 for r in runs if r.ok is False)
    rungs = engine.stats.units_started

    failures: list[str] = []
    if dead_air > 0:
        failures.append(f"silencio en antena: {dead_air:.0f} s")
    if rungs.get(RUNG_EMERGENCY, 0) > 0:
        failures.append(f"bucle de emergencia (peldaño 5): {rungs[RUNG_EMERGENCY]} veces")
    if back_to_back > 0:
        failures.append(f"mismo artista en canciones consecutivas: {back_to_back}")
    if len(signals) < expected:
        failures.append(f"señales horarias insuficientes: {len(signals)} < {expected}")
    if on_time < len(signals):
        failures.append(
            f"señales horarias tarde (> {max_late:.0f} s): {len(signals) - on_time}"
        )
    if max_ratio > grid.talk_budget.max_ratio + 1e-9:
        failures.append(
            f"presupuesto de charla superado: {max_ratio:.3f} > {grid.talk_budget.max_ratio}"
        )
    if fiction_after > 0:
        failures.append(f"ficción justo después de factual: {fiction_after}")
    if orphans > 0:
        failures.append(f"intros emitidas sin su canción detrás: {orphans}")
    if errors > 0:
        failures.append(f"producers con error: {errors}")

    sample = [
        Decision(
            at=a.started_at.astimezone(tz).strftime("%H:%M:%S"),
            kind=a.kind, title=a.title, reason=a.reason, rung=a.rung,
        )
        for a in items[:DECISIONS_SAMPLE]
    ]
    timeline = [
        TimelineEntry(
            at=a.started_at.astimezone(tz).strftime("%d %H:%M:%S"),
            kind=a.kind, title=a.title, duration_s=round(a.duration_s, 1), rung=a.rung,
            flags=", ".join(f for f, on in (
                ("interrupción", a.interrupt),
                ("cortado", a.cut),
                ("error", a.end_reason == "error"),
                ("fin de simulación", a.end_reason == "skipped" and not a.cut),
            ) if on),
        )
        for a in items
    ]
    return SimReport(
        seed=seed,
        hours=hours,
        mode=mode,
        catalog=catalog,
        start=start.isoformat(),
        end=end.isoformat(),
        segments_aired=len(aired),
        units_aired=sum(n for r, n in rungs.items() if r != RUNG_EMERGENCY),
        linked_units=linked,
        airtime_s={k: round(v, 2) for k, v in airtime.items()},
        airtime_pct=airtime_pct,
        music_share=round(airtime["music"] / total, 4),
        max_talk_ratio_rolling_hour=round(max_ratio, 4),
        talk_budget_ratio=grid.talk_budget.max_ratio,
        time_signals_aired=len(signals),
        time_signals_on_time=on_time,
        time_signals_expected_min=expected,
        interrupts=engine.stats.interrupts,
        music_cuts=engine.stats.music_cuts,
        back_to_back_artist=back_to_back,
        fiction_after_factual=fiction_after,
        orphan_intros=orphans,
        rung_histogram={str(r): rungs.get(r, 0) for r in range(1, RUNG_EMERGENCY + 1)},
        dead_air_s=round(dead_air, 3),
        producer_runs=len(runs),
        producer_errors=errors,
        gain_db={
            "measured": engine.stats.gain.count,
            "min": engine.stats.gain.min_db,
            "max": engine.stats.gain.max_db,
            "mean": (None if engine.stats.gain.mean_db is None
                     else round(engine.stats.gain.mean_db, 2)),
        },
        decisions_sample=sample,
        failures=failures,
        timeline=timeline,
    )
