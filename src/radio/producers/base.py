"""
Infraestructura común de los productores de Radio Parra (§4.2 de ARCHITECTURE.md).

Un productor = una clase = un ``kind``. Genera segmentos por adelantado (jobs
puntuales, invariante 2) y los deja en la BD con status ``ready``; la emisora solo
los lee. Este módulo define:

- ``ProducerContext``: dependencias inyectadas (BD, reloj, LLM, TTS, post, config).
- ``Producer``: el protocolo de §4.2 (``deficit`` + ``produce``).
- ``StagedProducer``: plantilla con el pipeline por etapas
  ``gather → write → validate → tts → post → register``, cada etapa sobreescribible
  y testeable por separado. Una etapa puede lanzar ``DraftRejected`` para descartar
  un borrador sin hacer fallar la ejecución; cualquier otra excepción la hace fallar
  (el runner la registra en ``producer_runs`` y no toca lo existente, §8).
  Un borrador puede registrarse como ``quarantined`` (``Draft.status``; §3.3:
  "validación falla 2× → quarantined") para revisión manual: se guarda con su audio
  pero no cuenta como creado ni como stock.
- ``call_llm``: llamada al LLM que suma coste y tokens a ``ctx.stats`` (también
  los de una llamada fallida pero facturada, ``LLMError.cost_eur``).
- ``register``: escritura atómica (§3.3): audio en ``data/tmp/`` → ``commit_audio``
  a ``data/stock/<kind>/`` → fila ``Segment`` + ``state_delta`` de ficción en la
  misma ``db.transaction()``. Nunca hay una fila ``ready`` sin su archivo completo.
- Regla de gasto (§4.2): ``budget_exhausted`` compara el gasto del mes (zona de la
  emisora) con ``station.budget.monthly_eur``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from radio.core.clock import Clock
from radio.core.config import ProducerSettings, RadioConfig
from radio.core.ids import new_id
from radio.core.models import (
    AudioInfo,
    LLMResult,
    Segment,
    SegmentKind,
    SourceDoc,
    Status,
    StockView,
    Voice,
)
from radio.core.paths import commit_audio, stock_dir, tmp_dir
from radio.core.store import DB
from radio.producers.post import AudioPost, NullPost
from radio.providers.errors import LLMError
from radio.providers.llm.base import LLM
from radio.providers.tts.base import TTS

logger = logging.getLogger(__name__)

# Error que se registra cuando la regla de gasto salta (§4.2)
BUDGET_EXHAUSTED = "presupuesto agotado"


class ProducerError(RuntimeError):
    """Fallo de configuración o de ejecución de un productor (se registra en producer_runs)."""


class DraftRejected(Exception):
    """Una etapa descarta el borrador actual; la ejecución sigue con el siguiente."""


# ── Contexto y contabilidad ──────────────────────────────────────────────────

@dataclass
class RunStats:
    """Consumo de una ejecución (columnas de ``producer_runs``)."""
    n_segments: int = 0
    rejected: int = 0
    quarantined: int = 0                # registrados en cuarentena (revisión manual)
    tokens_in: int = 0
    tokens_out: int = 0
    tts_chars: int = 0
    cost_eur: float = 0.0


@dataclass
class ProducerContext:
    """Dependencias que recibe cada productor al ejecutarse."""
    db: DB
    clock: Clock
    llm: LLM
    tts: TTS
    config: RadioConfig
    data_dir: Path                      # el audio va a data_dir/stock/<kind>/<id>.<ext>
    prompts_dir: Path = Path("prompts")
    # Postproducción (§4.2 paso 5). Por defecto no toca el audio; ``radio produce``
    # inyecta ``choose_post(config)`` (ffmpeg loudnorm si está instalado).
    post: AudioPost = field(default_factory=NullPost)
    # Contabilidad de la ejecución en curso (el runner da una nueva a cada productor)
    stats: RunStats = field(default_factory=RunStats)

    def for_run(self) -> ProducerContext:
        """Copia con la contabilidad a cero, para una ejecución."""
        return replace(self, stats=RunStats())


# ── Protocolo (§4.2) ──────────────────────────────────────────────────────────

@runtime_checkable
class Producer(Protocol):
    """Interfaz de un productor."""
    name: str                           # coincide con la clave en producers.yaml
    kind: SegmentKind
    factual: bool
    target_stock: int                   # cuántos segmentos ``ready`` quiere mantener
    # False si el productor no gasta dinero (la regla de gasto no le aplica)
    billable: bool

    def deficit(self, stock: StockView, now: datetime) -> int:
        """Cuántos segmentos faltan en ``stock`` para llegar al objetivo en ``now``."""
        ...

    def produce(self, ctx: ProducerContext) -> list[Segment]:
        """Genera y registra segmentos; devuelve los creados (status ``ready``)."""
        ...


# ── Borrador que recorre las etapas ──────────────────────────────────────────

@dataclass
class Draft:
    """Segmento en construcción. Cada etapa lo completa un poco más."""
    id: str = field(default_factory=new_id)
    sources: list[SourceDoc] = field(default_factory=list)
    script: str = ""
    voice: Voice | None = None
    audio: AudioInfo | None = None      # en data/tmp/ hasta ``register``
    ext: str = ".wav"                   # extensión del archivo final en el stock
    expires_at: datetime | None = None
    priority: int = 0
    parent_id: str | None = None
    prompt_version: str | None = None
    summary: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # Ficción (§6): cambios de estado del universo, aplicados al registrar
    universe: str | None = None
    state_delta: dict[str, Any] | None = None
    # Estado con el que se registra: ``ready`` o ``quarantined`` (revisión manual)
    status: Status = "ready"


# ── Plantilla por etapas ──────────────────────────────────────────────────────

class StagedProducer:
    """
    Plantilla de §4.2. Las subclases fijan ``name``/``kind``/``factual`` y
    sobreescriben las etapas que necesiten; por defecto:

    - ``gather``: nada (sin borradores).
    - ``write``: deja el guion como está.
    - ``validate``: guion no vacío si hay voz; devuelve la lista de problemas.
    - ``tts``: sintetiza ``script`` con ``voice`` en ``data/tmp/``.
    - ``post``: ``ctx.post`` (loudnorm + recorte de silencios, o nada).
    - ``register``: mueve el audio al stock e inserta el ``Segment``.

    Configuración: ``configure(config)`` lee su entrada de producers.yaml; se llama
    al construir (si se pasa config) y al empezar cada ``produce``.
    """

    name: str = ""
    kind: SegmentKind = ""
    factual: bool = False
    billable: bool = True
    default_target_stock: int = 0

    def __init__(self, config: RadioConfig | None = None) -> None:
        self.settings = ProducerSettings()
        self.target_stock = self.default_target_stock
        self.tz = ZoneInfo("Europe/Madrid")
        if config is not None:
            self.configure(config)

    # ── Configuración ─────────────────────────────────────────────────────────

    def configure(self, config: RadioConfig) -> None:
        """Lee ``producers.yaml → <name>`` y la zona horaria de la emisora."""
        self.settings = config.producers.get(self.name) or ProducerSettings()
        self.target_stock = self.settings.target_stock or self.default_target_stock
        self.tz = ZoneInfo(config.station.timezone)

    @property
    def params(self) -> dict[str, Any]:
        return self.settings.params

    # ── Protocolo ─────────────────────────────────────────────────────────────

    def deficit(self, stock: StockView, now: datetime) -> int:
        """Por defecto: ``target_stock`` menos los emitibles de ``kind``."""
        return max(0, self.target_stock - stock.count(self.kind))

    def produce(self, ctx: ProducerContext) -> list[Segment]:
        """Ejecuta ``prepare`` → etapas por borrador → ``finish``."""
        self.configure(ctx.config)
        now = ctx.clock.now()
        self.prepare(ctx, now)
        limit = self.how_many(self.deficit(ctx.db.stock_view(now), now))
        created: list[Segment] = []
        # ``gather`` puede devolver candidatos de reserva: se para al llegar al límite
        for draft in self.gather(ctx, limit):
            if len(created) >= limit:
                break
            seg = self.run_stages(ctx, draft)
            if seg is not None:
                created.append(seg)
        self.finish(ctx, created)
        return created

    def how_many(self, deficit: int) -> int:
        """Cuántos segmentos intentar crear en esta ejecución (por defecto, el déficit)."""
        return deficit

    def run_stages(self, ctx: ProducerContext, draft: Draft) -> Segment | None:
        """write → validate → tts → post → register para un borrador; None si se descarta."""
        try:
            draft = self.write(ctx, draft)
            problems = self.validate(ctx, draft)
            if problems:
                raise DraftRejected("; ".join(problems))
            draft = self.tts(ctx, draft)
            draft = self.post(ctx, draft)
            seg = self.register(ctx, draft)
            # En cuarentena queda registrado para revisión, pero no es stock creado
            return seg if seg.status == "ready" else None
        except DraftRejected as exc:
            ctx.stats.rejected += 1
            logger.warning("%s: borrador %s descartado: %s", self.name, draft.id, exc)
            self.on_rejected(ctx, draft, str(exc))
            return None
        finally:
            # Nada se queda en data/tmp/, pase lo que pase
            if draft.audio is not None and draft.audio.path.parent == tmp_dir(ctx.data_dir):
                draft.audio.path.unlink(missing_ok=True)

    # ── Ganchos y etapas ──────────────────────────────────────────────────────

    def prepare(self, ctx: ProducerContext, now: datetime) -> None:
        """Antes de calcular el déficit (p. ej. caducar lo vencido)."""

    def finish(self, ctx: ProducerContext, created: list[Segment]) -> None:
        """Después de registrar (p. ej. aplicar el tope de caché)."""

    def on_rejected(self, ctx: ProducerContext, draft: Draft, reason: str) -> None:
        """Un borrador se ha descartado (p. ej. para recordarlo o ponerlo en cuarentena)."""

    def gather(self, ctx: ProducerContext, wanted: int) -> list[Draft]:
        """
        Fuentes → borradores. ``wanted`` es cuántos segmentos se quieren; puede
        devolver más como reserva por si alguno se descarta.
        """
        return []

    def write(self, ctx: ProducerContext, draft: Draft) -> Draft:
        """Guion (en productores con LLM, aquí se llama al modelo)."""
        return draft

    def validate(self, ctx: ProducerContext, draft: Draft) -> list[str]:
        """Problemas del borrador (lista vacía = válido)."""
        if draft.voice is not None and not draft.script.strip():
            return ["guion vacío"]
        return []

    def tts(self, ctx: ProducerContext, draft: Draft) -> Draft:
        """Sintetiza ``draft.script`` en ``data/tmp/<kind>-<id>.wav``."""
        if draft.voice is None:
            return draft
        tmp = tmp_dir(ctx.data_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp / f"{self.kind}-{draft.id}.wav"
        # Se anota antes de sintetizar para que ``run_stages`` limpie si falla
        draft.audio = AudioInfo(path=tmp_path, duration_s=0.0)
        info = ctx.tts.synthesize(draft.script, draft.voice, tmp_path)
        # Solo lo sintetizado de verdad: un acierto de la caché de TTS no se factura
        if not info.cached:
            ctx.stats.tts_chars += len(draft.script)
        draft.audio = AudioInfo(path=tmp_path, duration_s=info.duration_s)
        draft.ext = ".wav"
        return draft

    def post(self, ctx: ProducerContext, draft: Draft) -> Draft:
        """Postproducción del audio temporal (``ctx.post``)."""
        if draft.audio is None:
            return draft
        before = draft.audio
        try:
            draft.audio = ctx.post.process(before)
        finally:
            if draft.audio.path != before.path:
                before.path.unlink(missing_ok=True)
        return draft

    def register(self, ctx: ProducerContext, draft: Draft) -> Segment:
        """
        Mueve el audio al stock e inserta el segmento (§3.3). La fila y el
        ``state_delta`` van en la misma transacción; si la inserción falla, se
        borra el archivo ya movido para no dejar huérfanos.
        """
        if draft.audio is None:
            raise ProducerError(f"{self.name}: borrador {draft.id} sin audio")
        if draft.audio.duration_s <= 0:
            raise DraftRejected(f"duración no válida: {draft.audio.duration_s}")
        final = stock_dir(ctx.data_dir, self.kind) / f"{draft.id}{draft.ext}"
        commit_audio(draft.audio.path, final)
        seg = self.build_segment(ctx, draft, final)
        try:
            with ctx.db.transaction():
                ctx.db.add_segment(seg)
                self.apply_state_delta(ctx, draft)
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        draft.audio = AudioInfo(path=final, duration_s=draft.audio.duration_s)
        if seg.status == "ready":
            ctx.stats.n_segments += 1
        else:
            ctx.stats.quarantined += 1
            logger.warning("%s: segmento %s registrado como %r para revisión manual",
                           self.name, seg.id, seg.status)
        return seg

    def build_segment(self, ctx: ProducerContext, draft: Draft, path: Path) -> Segment:
        """``Segment`` a partir del borrador (``meta`` incluye guion y fuentes)."""
        assert draft.audio is not None
        meta = dict(draft.meta)
        if draft.script:
            meta.setdefault("script", draft.script)
        if draft.sources:
            meta.setdefault(
                "sources", [{"id": s.id, "text": s.text, "url": s.url} for s in draft.sources]
            )
        return Segment(
            id=draft.id,
            kind=self.kind,
            factual=self.factual,
            path=path,
            duration_s=draft.audio.duration_s,
            created_at=ctx.clock.now(),
            producer=self.name,
            status=draft.status,
            expires_at=draft.expires_at,
            priority=draft.priority,
            parent_id=draft.parent_id,
            voice_id=draft.voice.id if draft.voice else None,
            prompt_version=draft.prompt_version,
            summary=draft.summary,
            meta=meta,
        )

    def apply_state_delta(self, ctx: ProducerContext, draft: Draft) -> None:
        """
        Ficción (§6): fusiona ``state_delta`` en ``universe_state`` (dentro de la
        transacción de ``register``). Por defecto, fusión superficial de claves.
        """
        if not draft.state_delta or not draft.universe:
            return
        current = ctx.db.get_universe_state(draft.universe)
        version, state = (None, {}) if current is None else current
        ctx.db.put_universe_state(
            draft.universe,
            {**state, **draft.state_delta},
            expected_version=version,
            updated_at=ctx.clock.now(),
        )


# ── LLM con contabilidad ──────────────────────────────────────────────────────

def call_llm(
    ctx: ProducerContext,
    system: str,
    user: str,
    *,
    temperature: float,
    json_schema: dict[str, Any] | None = None,
    max_tokens: int = 1000,
) -> LLMResult:
    """
    ``ctx.llm.complete`` sumando tokens y coste a ``ctx.stats`` (columnas de
    ``producer_runs`` y regla de gasto, §4.2). Si la llamada falla pero llegó a
    facturarse (``LLMError.cost_eur``: rechazo, respuesta cortada, JSON inválido), ese
    coste también se suma antes de propagar la excepción.
    """
    try:
        result = ctx.llm.complete(
            system, user, temperature=temperature, json_schema=json_schema,
            max_tokens=max_tokens,
        )
    except LLMError as exc:
        ctx.stats.tokens_in += exc.input_tokens
        ctx.stats.tokens_out += exc.output_tokens
        ctx.stats.cost_eur += exc.cost_eur
        raise
    ctx.stats.tokens_in += result.input_tokens
    ctx.stats.tokens_out += result.output_tokens
    ctx.stats.cost_eur += result.cost_eur
    return result


# ── Regla de gasto ────────────────────────────────────────────────────────────

def month_start(now: datetime, tz: ZoneInfo) -> datetime:
    """Inicio del mes de ``now`` en la zona ``tz`` (aware)."""
    local = now.astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def budget_exhausted(db: DB, config: RadioConfig, now: datetime) -> bool:
    """¿El gasto del mes (zona de la emisora) ya alcanza ``budget.monthly_eur``?"""
    start = month_start(now, ZoneInfo(config.station.timezone))
    return db.month_cost_eur(start) >= config.station.budget.monthly_eur


# ── Helpers ───────────────────────────────────────────────────────────────────

def segment_audio_path(ctx: ProducerContext, kind: str, seg_id: str) -> Path:
    """Ruta final del audio de un segmento: ``data/stock/<kind>/<id>.wav``."""
    return stock_dir(ctx.data_dir, kind) / f"{seg_id}.wav"


def write_segment_audio(
    ctx: ProducerContext,
    *,
    kind: str,
    seg_id: str,
    text: str,
    voice: Voice,
) -> AudioInfo:
    """
    Sintetiza ``text`` con ``voice`` de forma atómica: escribe en ``data/tmp/`` y
    mueve el archivo a ``data/stock/<kind>/`` con ``os.replace``. Si el TTS falla,
    no deja temporales a medias ni toca el stock. (Atajo sin pipeline.)
    """
    final_path = segment_audio_path(ctx, kind, seg_id)
    tmp = tmp_dir(ctx.data_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp / f"{kind}-{seg_id}.wav"
    try:
        info = ctx.tts.synthesize(text, voice, tmp_path)
        commit_audio(tmp_path, final_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return AudioInfo(path=final_path, duration_s=info.duration_s)


def pick_voice(config: RadioConfig, voice_id: str) -> Voice:
    """
    Devuelve la voz ``voice_id`` de voices.yaml.
    El consentimiento ya lo valida la configuración; aquí solo se exige que exista.
    """
    return config.voices.get(voice_id)
