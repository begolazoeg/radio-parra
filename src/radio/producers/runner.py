"""
Ejecución de productores como *jobs* (``radio produce``, timer de systemd), fuera de
la emisora (invariante 2).

``produce(ctx, names, dry_run)`` decide qué productores tocan, ejecuta cada uno de
forma aislada y registra cada ejecución en ``producer_runs`` (n_segments, tokens,
tts_chars, cost_eur, error). Un productor que falla no impide los demás y nunca toca
los segmentos ya existentes (§8): la siguiente pasada del timer lo reintenta.

Qué productores tocan
---------------------
- Con nombres explícitos (``radio produce NAME``): esos, estén o no activos y
  tengan o no déficit (útil para probar a mano un productor desactivado).
- Sin nombres (``radio produce --all``, lo que ejecuta el timer cada 15 min): los
  ``active`` de producers.yaml cuyo ``cron`` haya disparado desde su última
  ejecución **o** que tengan déficit (> 0). Así el timer global rellena huecos en
  cuanto aparecen (déficit) y el ``cron`` marca el ritmo de las tareas periódicas que
  no dependen del déficit (p. ej. ``music_tinydesk`` busca episodios nuevos aunque el
  stock esté lleno; ``time_signal`` caduca las señales vencidas).

Regla de gasto (§4.2): antes de producir, si el gasto del mes ya alcanza
``budget.monthly_eur``, el productor (si ``billable``) se salta y se registra una
ejecución ``ok=False`` con error "presupuesto agotado".

La emisora nunca ejecuta productores (invariante 2): ``radio simulate`` modela el
timer llamando a ``produce`` cada 15 min de tiempo simulado.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from radio.core.clock import Clock
from radio.core.config import RadioConfig
from radio.core.cron import cron_due
from radio.core.models import AudioInfo, LLMResult, Voice
from radio.core.store import DB
from radio.producers.base import (
    BUDGET_EXHAUSTED,
    Producer,
    ProducerContext,
    budget_exhausted,
)
from radio.producers.post import AudioPost, choose_post
from radio.producers.registry import PRODUCERS, build_producer
from radio.providers.registry import ProviderNotAvailable, build_llm, build_tts

logger = logging.getLogger(__name__)


# ── Informe ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunResult:
    """Resultado de un productor en una pasada."""
    name: str
    reason: str                          # por qué tocaba: "explícito", "cron", "déficit N"
    deficit: int
    ok: bool
    dry_run: bool = False
    segment_ids: tuple[str, ...] = ()
    rejected: int = 0
    quarantined: int = 0
    error: str | None = None


@dataclass
class ProduceReport:
    """Resultado de ``produce``: una entrada por productor que tocaba."""
    results: list[RunResult] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # nombre → por qué no toca

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def to_text(self) -> str:
        lines: list[str] = []
        for r in self.results:
            if r.dry_run:
                extra = "" if r.reason.startswith("déficit") else f"; déficit {r.deficit}"
                lines.append(f"{r.name:<16} tocaría ({r.reason}{extra})")
            elif r.ok:
                extra = f", {r.rejected} descartados" if r.rejected else ""
                extra += f", {r.quarantined} en cuarentena" if r.quarantined else ""
                lines.append(f"{r.name:<16} OK: {len(r.segment_ids)} segmentos{extra} ({r.reason})")
            else:
                lines.append(f"{r.name:<16} ERROR: {r.error} ({r.reason})")
        for name, why in self.skipped.items():
            lines.append(f"{name:<16} no toca: {why}")
        return "\n".join(lines) if lines else "Ningún productor configurado."


# ── Ejecución de un productor ─────────────────────────────────────────────────

def _local_now(ctx: ProducerContext) -> datetime:
    tz = ZoneInfo(ctx.config.station.timezone)
    now = ctx.clock.now()
    return now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)


def run_producer(ctx: ProducerContext, producer: Producer, *, reason: str = "", deficit: int = 0) -> RunResult:
    """
    Ejecuta un productor con su registro en ``producer_runs``. Nunca lanza
    (salvo ``KeyboardInterrupt``/``SystemExit``): los fallos quedan en el resultado.
    """
    now = _local_now(ctx)
    run_id = ctx.db.start_producer_run(producer.name, now)
    if producer.billable and budget_exhausted(ctx.db, ctx.config, now):
        logger.warning("%s: %s; se salta", producer.name, BUDGET_EXHAUSTED)
        ctx.db.finish_producer_run(run_id, ended_at=now, ok=False, error=BUDGET_EXHAUSTED)
        return RunResult(producer.name, reason, deficit, ok=False, error=BUDGET_EXHAUSTED)

    run_ctx = ctx.for_run()
    stats = run_ctx.stats
    error: str | None = None
    ids: tuple[str, ...] = ()
    try:
        created = producer.produce(run_ctx)
        ids = tuple(seg.id for seg in created)
    except Exception as exc:
        logger.exception("Productor %s ha fallado", producer.name)
        error = str(exc) or repr(exc)
    ctx.db.finish_producer_run(
        run_id,
        ended_at=_local_now(ctx),
        ok=error is None,
        n_segments=len(ids) if error is None else stats.n_segments,
        tokens_in=stats.tokens_in,
        tokens_out=stats.tokens_out,
        tts_chars=stats.tts_chars,
        cost_eur=stats.cost_eur,
        error=error,
    )
    return RunResult(
        producer.name, reason, deficit, ok=error is None,
        segment_ids=ids, rejected=stats.rejected, quarantined=stats.quarantined, error=error,
    )


def _cron_is_due(ctx: ProducerContext, name: str, now: datetime) -> bool:
    settings = ctx.config.producers.get(name)
    if settings is None or settings.cron is None:
        return False
    last = ctx.db.last_producer_run(name)
    return cron_due(settings.cron, None if last is None else last.started_at, now)


def produce(
    ctx: ProducerContext,
    names: Sequence[str] | None = None,
    *,
    dry_run: bool = False,
    producers: Mapping[str, Producer] | None = None,
) -> ProduceReport:
    """
    Ejecuta los productores que tocan (ver docstring del módulo). ``names`` None =
    todos los de producers.yaml. ``producers`` permite inyectar instancias (tests);
    si falta, se construyen con el registro ``PRODUCERS``.
    """
    config = ctx.config
    report = ProduceReport()
    explicit = names is not None
    candidates = list(names) if names is not None else list(config.producers.producers)
    now = _local_now(ctx)
    stock = ctx.db.stock_view(now)

    for name in candidates:
        producer = (producers or {}).get(name)
        if producer is None:
            if name not in PRODUCERS:
                if explicit:
                    report.results.append(RunResult(
                        name, "explícito", 0, ok=False, dry_run=dry_run,
                        error=f"productor desconocido (disponibles: {', '.join(sorted(PRODUCERS))})",
                    ))
                else:
                    report.skipped[name] = "no implementado"
                continue
            producer = build_producer(name, config)
        settings = config.producers.get(name)
        deficit = producer.deficit(stock, now)

        if explicit:
            reason = "explícito"
        elif settings is None or not settings.active:
            report.skipped[name] = "inactivo"
            continue
        elif _cron_is_due(ctx, name, now):
            reason = "cron"
        elif deficit > 0:
            reason = f"déficit {deficit}"
        else:
            report.skipped[name] = "sin déficit ni cron pendiente"
            continue

        if dry_run:
            report.results.append(RunResult(name, reason, deficit, ok=True, dry_run=True))
            continue
        report.results.append(run_producer(ctx, producer, reason=reason, deficit=deficit))
    return report


# ── Contexto para jobs ────────────────────────────────────────────────────────

class UnavailableLLM:
    """LLM que falla al usarse: el proveedor configurado no está disponible."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def preflight(self) -> None:
        raise ProviderNotAvailable(self.reason)

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1000,
    ) -> LLMResult:
        raise ProviderNotAvailable(self.reason)


class UnavailableTTS:
    """TTS que falla al usarse: el proveedor configurado no está disponible."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def preflight(self, voice: Voice) -> None:
        raise ProviderNotAvailable(self.reason)

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        raise ProviderNotAvailable(self.reason)


def build_context(
    config: RadioConfig,
    db: DB,
    clock: Clock,
    data_dir: Path,
    *,
    prompts_dir: Path = Path("prompts"),
    post: AudioPost | None = None,
) -> ProducerContext:
    """
    Contexto de producción real. Si el LLM o el TTS configurado no está
    disponible, se usa un sustituto que falla al llamarse: los productores que no
    lo necesitan (p. ej. música) funcionan y los demás registran el error.
    """
    providers = config.station.providers
    try:
        llm: Any = build_llm(providers.get("llm"))
    except ProviderNotAvailable as exc:
        llm = UnavailableLLM(str(exc))
    try:
        tts: Any = build_tts(providers.get("tts"))
    except ProviderNotAvailable as exc:
        tts = UnavailableTTS(str(exc))
    return ProducerContext(
        db=db,
        clock=clock,
        llm=llm,
        tts=tts,
        config=config,
        data_dir=data_dir,
        prompts_dir=prompts_dir,
        post=post if post is not None else choose_post(config),
    )
