"""
Productor ``host_intro`` (Fase 2, §12): la intro hablada del locutor IA antes de cada
concierto de Tiny Desk, **pegada a fuentes** (invariantes 4 y 5).

Decisiones de la dueña (2026-09-24, ADR 0003):

- Los datos salen **solo de fuentes abiertas**: MusicBrainz (datos centrales, CC0) y
  Wikipedia (CC BY-SA 4.0), vía ``radio.sources.gather_artist_sources``. El título del
  episodio se usa **solo como identificador**. La descripción del episodio de NPR
  **nunca** llega al LLM (términos de NPR: no usar su contenido para sistemas de IA):
  ni se guarda en la música ni este módulo lee otra cosa de su ``meta`` que el título
  y el artista.
- LLM: Claude Sonnet 5 (``providers.llm``); TTS: Piper local con la voz genérica
  ``locutor_principal`` (licencia del modelo pendiente de revisar por la dueña).

Pipeline (sobre ``StagedProducer``):

- ``prepare``: retira las intros ``ready`` cuya canción ya no está ``ready`` (§14:
  caducidad "ligada a la música"; la expulsión de la caché ya lo hace, esto recoge el
  resto, p. ej. una canción en cuarentena).
- ``deficit``: canciones emitibles sin intro ``ready``, con tope ``target_stock``
  menos las intros ``ready``.
- ``gather``: candidatas = música ``ready`` sin intro hija ``ready``/``quarantined``
  (una en cuarentena espera revisión: no se vuelve a pagar), primero las nunca
  emitidas y luego las emitidas hace más tiempo. Por cada una: artista (``meta`` o
  ``artist_from_title``), título y fuentes abiertas, con **un** ``RateLimiter`` y una
  ``SourceCache`` compartidos por toda la ejecución y un cliente HTTP con el
  ``User-Agent`` del proyecto.
- ``write``: plantillas Jinja de ``prompts/factual/`` (``PROMPT_VERSION``), salida
  JSON con esquema ``{script, claims: [{text, source_id}]}``. Escalera de §4.2 paso 3:

  1. intro con datos (o sin datos directamente si no hay fuentes);
  2. si no pasa la validación, **un** reintento con prompt más estricto que incluye
     los problemas (en español);
  3. si tampoco, **versión sin dato** (se exige ``script_has_facts`` False);
  4. si tampoco, se registra en ``quarantined`` (con audio, para revisión manual) y
     se cuenta en ``producer_runs`` / ``RunResult.quarantined``.

  Errores del LLM: rechazo, respuesta cortada o JSON inválido cuentan como intento
  fallido (su coste se suma igual); los demás (red, tasa, credenciales, petición
  inválida) hacen fallar la ejecución y el timer la reintenta (§8).
- ``validate``: ``check_grounding`` (fuentes citadas, datos del guion respaldados por
  claims) + forma: longitud (``max_chars`` ≈ 20 s leído), número de frases, idioma,
  sin marcado, sin URLs y sin afirmar ser humano.
- ``tts``: voz ``params.voice_id`` (``locutor_principal``, rol host); ``post``: el
  ``loudnorm`` de la palabra (``ctx.post``).
- ``register``: ``parent_id`` = la canción; ``meta``: title, script, claims, sources
  (``source_meta``: id, url, texto y licencia, para la atribución), grounding
  (resultado, problemas, términos permitidos), attempts, model, prompt_version,
  cost_eur y tokens.

``preview`` genera una sola intro sin registrarla (``radio preview host_intro``) y
``audit_host_intros`` comprueba sobre la BD que toda intro ``ready`` con datos tiene
claims trazables (``radio audit host_intro``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import jinja2

from radio.core.config import RadioConfig
from radio.core.models import Segment, SegmentKind, SourceDoc, StockView
from radio.core.store import DB
from radio.grounding import Claim, GroundingReport, check_grounding, script_has_facts
from radio.music.cache import retire_linked
from radio.music.feed import USER_AGENT, artist_from_title
from radio.producers.base import (
    Draft,
    DraftRejected,
    ProducerContext,
    StagedProducer,
    call_llm,
    pick_voice,
)
from radio.providers.errors import LLMInvalidOutput, LLMRefusal, LLMTruncated
from radio.sources import RateLimiter, SourceCache, gather_artist_sources, source_meta
from radio.sources.cache import DEFAULT_TTL_S

logger = logging.getLogger(__name__)

# Versión de las plantillas de prompts/factual/host_intro_*.j2 (Segment.prompt_version).
# Súbela al cambiar las plantillas o las reglas de validación.
PROMPT_VERSION = "host_intro/v1"

PROMPTS_SUBDIR = "factual"
SYSTEM_TEMPLATE = "host_intro_system.j2"
USER_TEMPLATE = "host_intro_user.j2"
RETRY_TEMPLATE = "host_intro_retry.j2"
FACT_FREE_TEMPLATE = "host_intro_fact_free.j2"

# Siempre permitidos sin fuente (además de emisora, artista y título del episodio)
FIXED_ALLOWED_TERMS: tuple[str, ...] = (
    "Tiny Desk", "Tiny Desk Concert", "Tiny Desk Concerts", "Tiny Desk de NPR",
    "NPR", "NPR Music",
)

# Salida estructurada (§4.2 paso 2): objetos cerrados, todo obligatorio
INTRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_id": {"type": "string"},
                },
                "required": ["text", "source_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["script", "claims"],
    "additionalProperties": False,
}

DEFAULTS: dict[str, Any] = {
    "max_chars": 320,          # ≈ 20 s leído a ~15 caracteres/s
    "min_chars": 40,
    "max_sentences": 4,        # el prompt pide 2–3; margen para abreviaturas ("EE. UU.")
    "max_audio_s": 40.0,       # tope del audio sintetizado (si no, cuarentena)
    "voice_id": "locutor_principal",
    "temperature": 0.3,        # Sonnet 5 la ignora (la rechaza la API); otros modelos no
    "max_tokens": 800,
    "sources_ttl_days": 30,
}

LANGUAGE_NAMES = {"es": "español", "ca": "catalán", "en": "inglés"}

Variant = Literal["grounded", "fact_free"]
Outcome = Literal["grounded", "fact_free", "quarantined"]

# Un recolector de fuentes: (artista, idioma) → documentos
SourceGatherer = Callable[[str, str], list[SourceDoc]]

# ── Comprobaciones de forma ───────────────────────────────────────────────────

_MARKUP_RE = re.compile(r"[<>{}\[\]*_#`|\\~^]")
_URL_RE = re.compile(
    r"(?:https?://|www\.|\b[\w-]+\.(?:com|org|net|es|io|fm|info|gov|edu)\b)", re.IGNORECASE
)
_HUMAN_RE = re.compile(
    r"\b(?:soy|como)\s+(?:un\s+|una\s+)?(?:persona|humano|humana|ser\s+humano)\b"
    r"|\bde\s+carne\s+y\s+hueso\b"
    r"|\bno\s+soy\s+(?:un\s+|una\s+)?(?:ia|inteligencia\s+artificial|m[aá]quina|robot)\b"
    r"|\bsoy\s+(?:un\s+|una\s+)?locutora?\s+(?:humano|humana|de\s+verdad|real)\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]*")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_ES_WORDS = frozenset(
    "el la los las de del que y en un una con por para su sus es al se lo desde ahora "
    "este esta aquí aqui nos os muy más mas sin sobre".split()
)
_EN_WORDS = frozenset(
    "the and of to is in with for this from her his their on at it was are be that".split()
)
# Texto de datos que no puede cerrar su bloque en el prompt (higiene ante inyección)
_DATA_TAG_RE = re.compile(r"<\s*/?\s*fuente", re.IGNORECASE)
MAX_FIELD_CHARS = 200


def language_problems(script: str, lang: str) -> list[str]:
    """Comprobación sencilla de idioma (solo ``es``): más palabras vacías españolas que inglesas."""
    if lang != "es":
        return []
    words = [w.casefold() for w in _WORD_RE.findall(script)]
    es = sum(1 for w in words if w in _ES_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    if es < 2 or en >= es:
        return ["el guion no parece estar en español"]
    return []


def count_sentences(script: str) -> int:
    """Frases del guion (trozos terminados en . ! ? …)."""
    return sum(1 for s in _SENTENCE_RE.findall(script) if s.strip(" .!?…\n\t"))


def form_problems(
    script: str, *, max_chars: int, min_chars: int, max_sentences: int, lang: str
) -> list[str]:
    """Problemas de forma de un guion (§4.2 paso 3: duración, idioma, sin marcado raro)."""
    text = script.strip()
    if not text:
        return ["guion vacío"]
    problems: list[str] = []
    if len(text) > max_chars:
        problems.append(f"el guion tiene {len(text)} caracteres; el máximo es {max_chars}")
    if len(text) < min_chars:
        problems.append(f"el guion es demasiado corto ({len(text)} caracteres)")
    sentences = count_sentences(text)
    if sentences > max_sentences:
        problems.append(f"el guion tiene {sentences} frases; deben ser 2 o 3")
    if _MARKUP_RE.search(text):
        problems.append("el guion contiene marcado o símbolos que no se pueden locutar")
    if _URL_RE.search(text):
        problems.append("el guion contiene una URL o dirección web")
    if _HUMAN_RE.search(text):
        problems.append("el guion afirma o insinúa que el locutor es humano (es una IA)")
    problems += language_problems(text, lang)
    return problems


def allowed_terms_for(station_name: str, artist: str | None, title: str | None) -> list[str]:
    """Términos que no necesitan fuente: emisora, artista, título del episodio, Tiny Desk, NPR."""
    terms = [station_name, *(t for t in (artist, title) if t), *FIXED_ALLOWED_TERMS]
    return list(dict.fromkeys(t.strip() for t in terms if t and t.strip()))


def data_text(text: str, limit: int | None = None) -> str:
    """Texto externo listo para un bloque de datos del prompt: sin etiquetas ``<fuente``."""
    clean = _DATA_TAG_RE.sub("‹fuente", text)
    return clean[:limit] if limit else clean


# ── Intentos del LLM ──────────────────────────────────────────────────────────

@dataclass
class Attempt:
    """Un intento de guion: respuesta del LLM y su verificación."""
    variant: Variant
    strict: bool
    script: str = ""
    claims: list[Claim] = field(default_factory=list)
    sources: list[SourceDoc] = field(default_factory=list)   # las que se le dieron
    problems: list[str] = field(default_factory=list)
    report: GroundingReport | None = None
    model: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.script.strip())

    def to_meta(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "strict": self.strict,
            "script": self.script,
            "claims": [{"text": c.text, "source_id": c.source_id} for c in self.claims],
            "problems": list(self.problems),
        }


@dataclass
class IntroContext:
    """Lo que se sabe de la canción para escribir su intro (y nada más de NPR)."""
    music_id: str
    title: str
    artist: str | None
    allowed_terms: list[str]


@dataclass
class PreviewResult:
    """Resultado de ``HostIntroProducer.preview``."""
    draft: Draft
    music: Segment
    audio_path: Path | None
    segment: Segment | None          # si se registró (``register=True``)
    cost_eur: float
    tokens_in: int
    tokens_out: int
    tts_chars: int


# ── Productor ─────────────────────────────────────────────────────────────────

class HostIntroProducer(StagedProducer):
    """Intros del locutor pegadas a fuentes abiertas, vinculadas a su canción."""
    name = "host_intro"
    kind: SegmentKind = "host_intro"
    factual = True
    billable = True
    default_target_stock = 10

    def __init__(
        self,
        config: RadioConfig | None = None,
        *,
        client: httpx.Client | None = None,
        source_gatherer: SourceGatherer | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(config)
        self._external_client = client
        self._external_gatherer = source_gatherer
        self._clock = clock
        self._sleep = sleep
        self._gatherer: SourceGatherer | None = None

    # ── Parámetros ────────────────────────────────────────────────────────────

    def param(self, key: str) -> Any:
        value = self.params.get(key)
        return DEFAULTS[key] if value is None else value

    # ── Sesión de fuentes (un limitador y una caché por ejecución) ────────────

    @contextmanager
    def sources_session(self, ctx: ProducerContext) -> Iterator[SourceGatherer]:
        """
        Recolector de fuentes para una ejecución: el inyectado o
        ``gather_artist_sources`` con un único ``RateLimiter``, la ``SourceCache`` de
        ``data/cache/sources/`` y un cliente HTTP con el ``User-Agent`` del proyecto.
        """
        if self._external_gatherer is not None:
            self._gatherer = self._external_gatherer
            try:
                yield self._gatherer
            finally:
                self._gatherer = None
            return
        own_client = self._external_client is None
        client = self._external_client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )
        limiter = RateLimiter(clock=self._clock, sleep=self._sleep)
        ttl_days = float(self.param("sources_ttl_days"))
        cache = SourceCache.for_data_dir(
            ctx.data_dir, ttl_s=ttl_days * 86400 if ttl_days > 0 else DEFAULT_TTL_S
        )

        def gather(artist: str, lang: str) -> list[SourceDoc]:
            return gather_artist_sources(
                artist, client=client, cache=cache, lang=lang, limiter=limiter
            )

        self._gatherer = gather
        try:
            yield gather
        finally:
            self._gatherer = None
            if own_client:
                client.close()

    def produce(self, ctx: ProducerContext) -> list[Segment]:
        self.configure(ctx.config)
        with self.sources_session(ctx):
            return super().produce(ctx)

    # ── Déficit y candidatas ──────────────────────────────────────────────────

    def deficit(self, stock: StockView, now: datetime) -> int:
        intros = stock.get(self.kind)
        with_intro = {s.parent_id for s in intros if s.parent_id}
        missing = sum(1 for m in stock.get("music") if m.id not in with_intro)
        return max(0, min(missing, self.target_stock - len(intros)))

    def prepare(self, ctx: ProducerContext, now: datetime) -> None:
        """Retira intros ``ready`` cuya canción ya no está ``ready`` (ligadas a la música)."""
        parents: dict[str, bool] = {}
        for intro in ctx.db.list_segments(kind=self.kind, status="ready"):
            pid = intro.parent_id
            if pid is None:
                continue
            if pid not in parents:
                parent = ctx.db.get_segment(pid)
                parents[pid] = parent is not None and parent.is_live(now)
            if not parents[pid]:
                retire_linked(ctx.db, pid, kinds=(self.kind,))

    def candidates(self, ctx: ProducerContext, now: datetime) -> list[Segment]:
        """
        Música emitible sin intro ``ready`` ni en revisión (``quarantined`` /
        ``pending_review``): primero la nunca emitida (la más antigua antes) y después
        la emitida hace más tiempo.
        """
        music = list(ctx.db.stock_view(now).get("music"))
        if not music:
            return []
        busy = {
            s.parent_id for s in ctx.db.list_segments(kind=self.kind)
            if s.parent_id and s.status in ("ready", "quarantined", "pending_review")
        }
        music = [m for m in music if m.id not in busy]
        if not music:
            return []
        ids = {m.id for m in music}
        last_play: dict[str, datetime] = {}
        for entry in ctx.db.list_play_log(since=min(m.created_at for m in music)):
            if entry.segment_id in ids:
                prev = last_play.get(entry.segment_id)
                if prev is None or entry.started_at > prev:
                    last_play[entry.segment_id] = entry.started_at
        return sorted(music, key=lambda m: (
            m.id in last_play, last_play.get(m.id, m.created_at), m.created_at, m.id,
        ))

    # ── gather ────────────────────────────────────────────────────────────────

    def intro_context(self, ctx: ProducerContext, music: Segment) -> IntroContext:
        """
        Artista y título de la canción. De ``music.meta`` solo se leen ``title`` y
        ``artist``: nada más del episodio de NPR llega al LLM (ADR 0003).
        """
        title = str(music.meta.get("title") or "").strip()
        artist_raw = music.meta.get("artist")
        artist = str(artist_raw).strip() if artist_raw else None
        if not artist and title:
            artist = artist_from_title(title)
        return IntroContext(
            music_id=music.id,
            title=title,
            artist=artist or None,
            allowed_terms=allowed_terms_for(ctx.config.station.name, artist, title),
        )

    def draft_for(self, ctx: ProducerContext, music: Segment) -> Draft:
        """Borrador de la intro de ``music`` con sus fuentes abiertas."""
        info = self.intro_context(ctx, music)
        gatherer = self._gatherer
        if gatherer is None:
            raise RuntimeError("fuentes no inicializadas (usa produce() o preview())")
        sources = gatherer(info.artist, ctx.config.station.language) if info.artist else []
        return Draft(
            sources=list(sources),
            voice=pick_voice(ctx.config, str(self.param("voice_id"))),
            parent_id=music.id,
            prompt_version=PROMPT_VERSION,
            meta={
                "title": f"Intro: {info.title or music.title}",
                "music_title": info.title,
                "artist": info.artist,
                "station": ctx.config.station.name,
                "lang": ctx.config.station.language,
            },
        )

    def gather(self, ctx: ProducerContext, wanted: int) -> list[Draft]:
        if wanted <= 0:
            return []
        now = ctx.clock.now()
        chosen = self.candidates(ctx, now)[:wanted]
        drafts = [self.draft_for(ctx, m) for m in chosen]
        logger.info("%s: %d intros por escribir (%d con fuentes)", self.name, len(drafts),
                    sum(1 for d in drafts if d.sources))
        return drafts

    # ── write: escalera de intentos ───────────────────────────────────────────

    def _env(self, ctx: ProducerContext) -> jinja2.Environment:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ctx.prompts_dir / PROMPTS_SUBDIR)),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["data"] = data_text
        return env

    def render_prompts(
        self,
        ctx: ProducerContext,
        draft: Draft,
        *,
        variant: Variant,
        strict: bool,
        problems: Sequence[str] = (),
    ) -> tuple[str, str]:
        """(system, user) de un intento. Solo artista, título y fuentes abiertas."""
        env = self._env(ctx)
        lang = str(draft.meta.get("lang") or ctx.config.station.language)
        values: dict[str, Any] = {
            "station_name": ctx.config.station.name,
            "language": lang,
            "language_name": LANGUAGE_NAMES.get(lang, lang),
            "max_chars": int(self.param("max_chars")),
            "artist": data_text(str(draft.meta.get("artist") or ""), MAX_FIELD_CHARS),
            "episode_title": data_text(str(draft.meta.get("music_title") or ""), MAX_FIELD_CHARS),
            "sources": [] if variant == "fact_free" else draft.sources,
            "fact_free": variant == "fact_free",
            "problems": list(problems),
        }
        system = env.get_template(SYSTEM_TEMPLATE).render(**values)
        if strict:
            template = RETRY_TEMPLATE
        elif variant == "fact_free":
            template = FACT_FREE_TEMPLATE
        else:
            template = USER_TEMPLATE
        user = env.get_template(template).render(**values)
        return system, user

    def attempt(
        self,
        ctx: ProducerContext,
        draft: Draft,
        *,
        variant: Variant,
        strict: bool,
        problems: Sequence[str] = (),
    ) -> Attempt:
        """Un intento: prompts → LLM → JSON → validación."""
        system, user = self.render_prompts(
            ctx, draft, variant=variant, strict=strict, problems=problems
        )
        sources = [] if variant == "fact_free" else list(draft.sources)
        att = Attempt(variant=variant, strict=strict, sources=sources)
        try:
            result = call_llm(
                ctx, system, user,
                temperature=float(self.param("temperature")),
                json_schema=INTRO_SCHEMA,
                max_tokens=int(self.param("max_tokens")),
            )
        except (LLMRefusal, LLMTruncated, LLMInvalidOutput) as exc:
            att.problems = [f"el LLM no dio un guion utilizable: {exc}"]
            return att
        att.model = result.model
        try:
            data = json.loads(result.text)
            if not isinstance(data, dict):
                raise ValueError("no es un objeto JSON")
            script = data.get("script")
            raw_claims = data.get("claims", [])
            if not isinstance(script, str) or not isinstance(raw_claims, list):
                raise ValueError("faltan 'script' o 'claims'")
            att.script = script.strip()
            att.claims = [Claim.from_dict(c) for c in raw_claims]
        except (ValueError, TypeError, AttributeError) as exc:
            att.problems = [f"respuesta mal formada: {exc}"]
            return att
        att.problems = self.check(ctx, draft, att)
        return att

    def check(self, ctx: ProducerContext, draft: Draft, att: Attempt) -> list[str]:
        """Validación de un intento (forma + grounding; ver docstring del módulo)."""
        lang = str(draft.meta.get("lang") or ctx.config.station.language)
        allowed = self.allowed_terms(ctx, draft)
        problems = form_problems(
            att.script,
            max_chars=int(self.param("max_chars")),
            min_chars=int(self.param("min_chars")),
            max_sentences=int(self.param("max_sentences")),
            lang=lang,
        )
        if att.variant == "fact_free":
            if att.claims:
                problems.append("la versión sin datos no debe llevar claims")
            report = check_grounding(att.script, [], [], lang=lang, allowed_terms=allowed)
        else:
            report = check_grounding(
                att.script, att.claims, att.sources, lang=lang, allowed_terms=allowed
            )
        att.report = report
        problems += list(report.problems)
        return problems

    def allowed_terms(self, ctx: ProducerContext, draft: Draft) -> list[str]:
        return allowed_terms_for(
            ctx.config.station.name,
            draft.meta.get("artist") or None,
            draft.meta.get("music_title") or None,
        )

    def write(self, ctx: ProducerContext, draft: Draft) -> Draft:
        """
        Escalera: con datos (o sin datos si no hay fuentes) → reintento estricto con
        los problemas → versión sin dato → cuarentena. Deja el guion elegido en
        ``draft`` y todo lo verificado en ``draft.meta``.
        """
        cost0, tin0, tout0 = ctx.stats.cost_eur, ctx.stats.tokens_in, ctx.stats.tokens_out
        first: Variant = "grounded" if draft.sources else "fact_free"
        attempts = [self.attempt(ctx, draft, variant=first, strict=False)]
        if not attempts[-1].ok:
            attempts.append(self.attempt(
                ctx, draft, variant=first, strict=True, problems=attempts[-1].problems,
            ))
        if not attempts[-1].ok and first != "fact_free":
            attempts.append(self.attempt(
                ctx, draft, variant="fact_free", strict=True, problems=attempts[-1].problems,
            ))
        chosen = next((a for a in attempts if a.ok), None)
        outcome: Outcome
        if chosen is not None:
            outcome = chosen.variant
        else:
            # Para revisión: el último intento con guion
            chosen = next((a for a in reversed(attempts) if a.script.strip()), None)
            if chosen is None:
                draft.meta["attempts"] = [a.to_meta() for a in attempts]
                raise DraftRejected(
                    "ningún intento dio un guion: " + "; ".join(attempts[-1].problems)
                )
            outcome = "quarantined"
            draft.status = "quarantined"

        self.apply_attempt(ctx, draft, chosen, outcome=outcome, attempts=attempts)
        draft.meta["cost_eur"] = round(ctx.stats.cost_eur - cost0, 6)
        draft.meta["tokens_in"] = ctx.stats.tokens_in - tin0
        draft.meta["tokens_out"] = ctx.stats.tokens_out - tout0
        return draft

    def apply_attempt(
        self,
        ctx: ProducerContext,
        draft: Draft,
        att: Attempt,
        *,
        outcome: Outcome,
        attempts: Sequence[Attempt],
    ) -> None:
        """Pasa el intento elegido al borrador (guion, claims, fuentes, informe)."""
        lang = str(draft.meta.get("lang") or ctx.config.station.language)
        allowed = self.allowed_terms(ctx, draft)
        report = att.report
        draft.script = att.script
        draft.meta.update({
            "script": att.script,
            "claims": [{"text": c.text, "source_id": c.source_id} for c in att.claims],
            # Fuentes con las que se escribió este guion (atribución: url + licencia)
            "sources": [source_meta(s) for s in att.sources],
            "gathered_sources": [s.id for s in draft.sources],
            "model": att.model,
            "grounding": {
                "outcome": outcome,
                "ok": outcome != "quarantined",
                "has_facts": script_has_facts(att.script, lang=lang, allowed_terms=allowed),
                "problems": list(att.problems),
                "unsupported": list(report.unsupported) if report else [],
                "checked_facts": [f.text for f in report.checked_facts] if report else [],
                "allowed_terms": allowed,
                "lang": lang,
                "attempts": len(attempts),
            },
            "attempts": [a.to_meta() for a in attempts],
        })
        who = draft.meta.get("artist") or draft.meta.get("music_title") or "concierto"
        label = {"grounded": "con datos" if draft.meta["grounding"]["has_facts"] else "sin datos",
                 "fact_free": "sin datos", "quarantined": "en cuarentena"}[outcome]
        draft.summary = f"Intro de {who} ({label})"

    # ── validate / tts ────────────────────────────────────────────────────────

    def validate(self, ctx: ProducerContext, draft: Draft) -> list[str]:
        """
        Revalida el guion elegido. Uno en cuarentena ya se sabe que falla: sigue hasta
        el TTS para que la revisión manual tenga audio.
        """
        problems = super().validate(ctx, draft)
        if problems or draft.status == "quarantined":
            return problems
        grounding = draft.meta.get("grounding", {})
        att = Attempt(
            variant="fact_free" if grounding.get("outcome") == "fact_free" else "grounded",
            strict=False,
            script=draft.script,
            claims=[Claim.from_dict(c) for c in draft.meta.get("claims", [])],
            sources=[s for s in draft.sources
                     if s.id in {m["id"] for m in draft.meta.get("sources", [])}],
        )
        return self.check(ctx, draft, att)

    def tts(self, ctx: ProducerContext, draft: Draft) -> Draft:
        draft = super().tts(ctx, draft)
        max_audio = float(self.param("max_audio_s"))
        if draft.audio is not None and draft.audio.duration_s > max_audio:
            problem = f"audio de {draft.audio.duration_s:.1f} s (máximo {max_audio:.0f} s)"
            logger.warning("%s: %s → cuarentena", self.name, problem)
            draft.status = "quarantined"
            grounding = draft.meta.setdefault("grounding", {})
            grounding["ok"] = False
            grounding["outcome"] = "quarantined"
            grounding.setdefault("problems", []).append(problem)
        return draft

    # ── preview (radio preview host_intro) ────────────────────────────────────

    def preview(
        self,
        ctx: ProducerContext,
        music: Segment | None = None,
        *,
        register: bool = False,
        out_path: Path | None = None,
    ) -> PreviewResult:
        """
        Una sola intro para ``music`` (o la siguiente candidata) **sin registrarla**
        salvo ``register=True``. El audio queda en ``out_path`` (o en ``data/tmp/``;
        lo borra quien llama). Lanza ``DraftRejected`` si no se pudo escribir.
        """
        self.configure(ctx.config)
        now = ctx.clock.now()
        if music is None:
            candidates = self.candidates(ctx, now)
            if not candidates:
                raise LookupError("no hay música 'ready' sin intro para previsualizar")
            music = candidates[0]
        stats0 = (ctx.stats.cost_eur, ctx.stats.tokens_in, ctx.stats.tokens_out,
                  ctx.stats.tts_chars)
        with self.sources_session(ctx):
            draft = self.draft_for(ctx, music)
        draft = self.write(ctx, draft)
        problems = self.validate(ctx, draft)
        if problems:
            draft.status = "quarantined"
            draft.meta.setdefault("grounding", {})["problems"] = problems
        draft = self.tts(ctx, draft)
        draft = self.post(ctx, draft)
        segment: Segment | None = None
        audio_path: Path | None = draft.audio.path if draft.audio else None
        if register:
            segment = self.register(ctx, draft)
            audio_path = segment.path
        elif draft.audio is not None and out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            draft.audio.path.replace(out_path)
            audio_path = out_path
        return PreviewResult(
            draft=draft, music=music, audio_path=audio_path, segment=segment,
            cost_eur=ctx.stats.cost_eur - stats0[0],
            tokens_in=ctx.stats.tokens_in - stats0[1],
            tokens_out=ctx.stats.tokens_out - stats0[2],
            tts_chars=ctx.stats.tts_chars - stats0[3],
        )


# ── Auditoría (radio audit host_intro) ────────────────────────────────────────

@dataclass(frozen=True)
class AuditViolation:
    """Una intro ``ready`` que incumple la trazabilidad de sus datos."""
    segment_id: str
    title: str
    problem: str

    def to_text(self) -> str:
        return f"{self.segment_id} ({self.title}): {self.problem}"


def audit_segment(seg: Segment, station_name: str) -> list[AuditViolation]:
    """
    Comprueba una intro: si su guion tiene datos, al menos un claim; cada claim cita
    una fuente de ``meta["sources"]`` (con URL para la atribución) y
    ``check_grounding`` vuelve a pasar con lo guardado.
    """
    meta = seg.meta
    out: list[AuditViolation] = []

    def bad(problem: str) -> None:
        out.append(AuditViolation(seg.id, seg.title, problem))

    script = str(meta.get("script") or "")
    lang = str(meta.get("lang") or meta.get("grounding", {}).get("lang") or "es")
    allowed = allowed_terms_for(
        str(meta.get("station") or station_name),
        meta.get("artist") or None,
        meta.get("music_title") or None,
    )
    if not script.strip():
        bad("sin guion en meta")
        return out
    if not seg.parent_id:
        bad("sin parent_id (debe ir ligada a una canción)")
    if not seg.prompt_version:
        bad("sin prompt_version")
    try:
        claims = [Claim.from_dict(c) for c in meta.get("claims", [])]
    except (ValueError, TypeError, AttributeError) as exc:
        bad(f"claims ilegibles: {exc}")
        return out
    raw_sources = meta.get("sources", [])
    sources = [
        SourceDoc(id=str(s.get("id")), text=str(s.get("text") or ""), url=str(s.get("url") or ""))
        for s in raw_sources if isinstance(s, dict)
    ]
    ids = {s.id for s in sources}
    has_facts = script_has_facts(script, lang=lang, allowed_terms=allowed)
    if has_facts and not claims:
        bad("el guion tiene datos y ningún claim")
    for n, claim in enumerate(claims, start=1):
        if claim.source_id not in ids:
            bad(f"claim {n} cita {claim.source_id!r}, que no está en meta['sources']")
    for s in sources:
        if s.id in {c.source_id for c in claims} and not s.url:
            bad(f"la fuente {s.id!r} no tiene URL (atribución)")
    report = check_grounding(script, claims, sources, lang=lang, allowed_terms=allowed)
    for problem in report.problems:
        bad(f"grounding: {problem}")
    return out


@dataclass
class AuditReport:
    """Resultado de ``audit_host_intros``."""
    checked: int = 0
    with_facts: int = 0
    violations: list[AuditViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_text(self) -> str:
        lines = [
            f"Intros 'ready' revisadas: {self.checked} ({self.with_facts} con datos)",
            f"Incumplimientos: {len(self.violations)}",
        ]
        lines += [f"  - {v.to_text()}" for v in self.violations]
        lines.append("RESULTADO: OK" if self.ok else "RESULTADO: FALLO")
        return "\n".join(lines)


def audit_host_intros(db: DB, station_name: str, *, kind: str = "host_intro") -> AuditReport:
    """Audita todas las intros ``ready``: el 100 % de las que tienen datos, trazables."""
    report = AuditReport()
    for seg in db.list_segments(kind=kind, status="ready"):
        report.checked += 1
        meta = seg.meta
        lang = str(meta.get("lang") or "es")
        allowed = allowed_terms_for(
            str(meta.get("station") or station_name),
            meta.get("artist") or None, meta.get("music_title") or None,
        )
        if script_has_facts(str(meta.get("script") or ""), lang=lang, allowed_terms=allowed):
            report.with_facts += 1
        report.violations.extend(audit_segment(seg, station_name))
    return report
