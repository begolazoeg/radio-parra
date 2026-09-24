"""
Modelos de dominio de Radio Parra (§3.1 de ARCHITECTURE.md).
Todos los dataclasses son frozen=True para garantizar inmutabilidad.

Convenciones de ``Segment.meta`` (lo que antes eran columnas propias):

- ``meta["title"]``: título legible (str).
- ``meta["tags"]``: etiquetas libres (list[str]), p. ej. ``"artist:<slug>"``,
  ``"hour:YYYY-MM-DDTHH"`` o ``"source:tiny_desk"``.
- ``meta["script"]``: guion locutado (str).
- ``meta["sources"]``: fuentes con las que se escribió (list[dict] con id/text/url).
- ``meta["guid"]``: identificador externo para deduplicar (p. ej. item RSS).
- ``meta["loudness_lufs"]`` / ``meta["true_peak_db"]``: medida de loudness del audio
  (float o None) para la ganancia en reproducción (``radio.station.gain``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args

# ── Tipos base ────────────────────────────────────────────────────────────────

# Estados posibles de un segmento a lo largo de su ciclo de vida (§3.3)
Status = Literal["ready", "pending_review", "quarantined", "expired", "retired"]
STATUSES: frozenset[str] = frozenset(get_args(Status))

# Tipo de segmento. Es abierto (§14): cada productor declara el suyo.
SegmentKind = str

# Kinds conocidos del catálogo (§14); solo orientativo, no restringe nada
KNOWN_KINDS: frozenset[str] = frozenset({
    "music", "host_intro", "time_signal", "jingle", "stinger",
    "weather", "ephemeris", "sky", "word_of_day",
    "consultorio", "liga", "horoscope", "interview",
    "pueblo", "radionovela", "teletienda", "lost_found", "contest", "ads_parody",
    "news", "agenda", "birthday", "dedication", "voicemail", "serial_classic",
    "parra_report",
})

# Estados de un mensaje del inbox (§3.2)
InboxStatus = Literal["pending", "approved", "rejected", "used"]
INBOX_STATUSES: frozenset[str] = frozenset(get_args(InboxStatus))


# ── Documentos fuente y resultados de proveedores ────────────────────────────

@dataclass(frozen=True)
class SourceDoc:
    """Documento fuente para alimentar al LLM (artículo, RSS item, etc.)."""
    id: str
    text: str
    url: str


@dataclass(frozen=True)
class LLMResult:
    """
    Respuesta cruda de un proveedor LLM. ``model`` es el id que respondió y
    ``cost_eur`` el coste estimado de la llamada (lo suma el productor en
    ``producer_runs.cost_eur`` para la regla de gasto, §4.2); los fakes dejan 0.
    """
    text: str
    input_tokens: int
    output_tokens: int
    model: str = ""
    cost_eur: float = 0.0


@dataclass(frozen=True)
class AudioInfo:
    """
    Metadatos de un archivo de audio ya generado o importado. ``cached`` es True si
    un TTS lo ha servido desde su caché (``CachedTTS``) sin sintetizar ni facturar:
    esos caracteres no cuentan en ``producer_runs.tts_chars``.
    """
    path: Path
    duration_s: float
    cached: bool = False


# ── Segmento principal ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Segment:
    """
    Unidad atómica de emisión (§3.1). Las fechas son siempre *aware*.
    ``path`` apunta a un audio ya completo en disco (escritura atómica, §3.3).
    """
    id: str                              # ULID
    kind: SegmentKind                    # "music" | "time_signal" | "weather" | ...
    factual: bool                        # separa datos reales de ficción
    path: Path                           # audio ya listo en disco
    duration_s: float
    created_at: datetime
    producer: str                        # nombre del productor que lo creó
    status: Status = "ready"
    expires_at: datetime | None = None   # noticias sí; poema clásico no
    priority: int = 0                    # >0 puede interrumpir (p. ej. señal horaria)
    parent_id: str | None = None         # host_intro -> música a la que precede
    voice_id: str | None = None
    prompt_version: str | None = None
    summary: str | None = None           # resumen corto: alimenta la memoria de ficción
    meta: dict[str, Any] = field(default_factory=dict)  # fuentes, semillas, modelo, etc.

    @property
    def title(self) -> str:
        """Título legible (``meta["title"]``); si falta, el kind."""
        value = self.meta.get("title")
        return str(value) if value else self.kind

    @property
    def tags(self) -> tuple[str, ...]:
        """Etiquetas de ``meta["tags"]`` (tupla vacía si no hay)."""
        return tuple(str(t) for t in self.meta.get("tags", ()))

    def is_live(self, now: datetime) -> bool:
        """``ready`` y sin caducar en ``now``."""
        return self.status == "ready" and (self.expires_at is None or self.expires_at > now)


# ── Stock ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StockView:
    """
    Instantánea del stock emitible: segmentos ``ready`` y no caducados en un
    instante dado, agrupados por kind (el orden de cada tupla es el del store).
    """
    by_kind: Mapping[str, tuple[Segment, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def from_segments(cls, segments: Iterable[Segment], now: datetime) -> StockView:
        """Construye la vista filtrando lo que no está vivo en ``now``."""
        grouped: dict[str, list[Segment]] = {}
        for seg in segments:
            if seg.is_live(now):
                grouped.setdefault(seg.kind, []).append(seg)
        return cls(MappingProxyType({k: tuple(v) for k, v in grouped.items()}))

    def count(self, kind: str) -> int:
        """Número de segmentos emitibles de ``kind``."""
        return len(self.by_kind.get(kind, ()))

    def get(self, kind: str) -> tuple[Segment, ...]:
        """Segmentos emitibles de ``kind`` (tupla vacía si no hay)."""
        return self.by_kind.get(kind, ())

    def kinds(self) -> set[str]:
        """Kinds con al menos un segmento emitible."""
        return {k for k, v in self.by_kind.items() if v}


# ── Registros del store ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayLogEntry:
    """Fila de ``play_log``: una emisión (``segment_id`` None = fuera de stock)."""
    id: int
    segment_id: str | None
    kind: str
    mode: str
    started_at: datetime
    ended_at: datetime | None
    skipped: bool
    duration_s: float | None = None      # duración nominal del segmento, si existe


@dataclass(frozen=True)
class ProducerRun:
    """Fila de ``producer_runs``: una ejecución de un productor con su coste."""
    id: int
    producer: str
    started_at: datetime
    ended_at: datetime | None
    ok: bool | None                      # None = en curso o interrumpida
    n_segments: int
    tokens_in: int
    tokens_out: int
    tts_chars: int
    cost_eur: float
    error: str | None


@dataclass(frozen=True)
class SignalReading:
    """Lectura de un sensor u otra fuente externa (tabla ``signals``)."""
    source: str
    key: str
    value: str
    at: datetime


@dataclass(frozen=True)
class InboxItem:
    """Mensaje entrante (dedicatoria, nota de voz...) pendiente de moderación."""
    id: int
    channel: str
    sender: str
    payload: str
    status: InboxStatus
    created_at: datetime


# ── Voces ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Voice:
    """
    Voz disponible para TTS (§5). ``consent`` debe ser True y ``consent_note``
    no vacío; lo garantiza la validación de voices.yaml (``VoiceEntry``).
    """
    id: str
    role: str                    # "host" | "character" | ...
    provider: str                # "cloud" | "local" | "fake" | ...
    provider_voice_id: str       # id de la voz en el proveedor
    consent: bool
    consent_note: str
    language: str = "es"
    universe: str | None = None  # universo de ficción, si es un personaje
    description: str = ""
