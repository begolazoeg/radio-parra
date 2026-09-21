"""
Modelos de dominio de Radio Parra.
Todos los dataclasses son frozen=True para garantizar inmutabilidad.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

# ── Tipos base ────────────────────────────────────────────────────────────────

# Estados posibles de un segmento a lo largo de su ciclo de vida
SegmentStatus = Literal["pending", "ready", "playing", "done", "error"]

# Tipos de segmento que puede emitir la radio
SegmentKind = Literal["music", "host_intro", "factual", "fiction", "time_signal", "jingle"]


# ── Documentos fuente y resultados de proveedores ────────────────────────────

@dataclass(frozen=True)
class SourceDoc:
    """Documento fuente para alimentar al LLM (artículo, RSS item, etc.)."""
    id: str
    text: str
    url: str


@dataclass(frozen=True)
class LLMResult:
    """Respuesta cruda de un proveedor LLM."""
    text: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class AudioInfo:
    """Metadatos de un archivo de audio ya generado o importado."""
    path: Path
    duration_s: float


# ── Segmento principal ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Segment:
    """
    Unidad atómica de emisión.
    Corresponde a §3.1 de ARCHITECTURE.md.
    """
    id: str                          # ULID
    kind: SegmentKind
    status: SegmentStatus
    created_at: datetime
    title: str
    duration_s: float
    audio_path: Path | None          # None hasta que el audio esté listo
    producer: str                    # nombre del producer que lo generó
    # Metadatos opcionales por tipo
    source_url: str | None = None    # para factual / music
    script: str | None = None        # guion generado por LLM
    voice_id: str | None = None      # voz usada en TTS
    tags: tuple[str, ...] = ()       # etiquetas libres


# ── Configuración de voces ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Voice:
    """
    Voz disponible para TTS.
    El campo consent=True es obligatorio (validado en VoicesConfig).
    """
    id: str
    name: str
    provider: str       # "elevenlabs" | "local" | ...
    consent: bool       # debe ser True; rechazado si False
    language: str = "es"
    description: str = ""
