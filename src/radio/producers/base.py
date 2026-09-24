"""
Infraestructura común de los producers de Radio Parra.

Un producer genera segmentos (guion + audio) y los deja en la BD con
status "ready" para que el scheduler los emita. Este módulo define el
contexto compartido, el protocolo Producer y helpers de escritura atómica.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from radio.core.clock import Clock
from radio.core.config import RadioConfig, VoiceEntry
from radio.core.models import AudioInfo, SegmentKind
from radio.core.store import DB
from radio.providers.llm.base import LLM
from radio.providers.tts.base import TTS

# Nombre de la emisora tal y como se pronuncia en antena
STATION_NAME = "Radio Parra"


# ── Contexto y protocolo ──────────────────────────────────────────────────────

@dataclass
class ProducerContext:
    """Dependencias que recibe cada producer al ejecutarse."""
    db: DB
    clock: Clock
    llm: LLM
    tts: TTS
    config: RadioConfig
    data_dir: Path                      # el audio va a data_dir/segments/<kind>/<id>.wav
    prompts_dir: Path = Path("prompts")


class Producer(Protocol):
    """Interfaz mínima de un producer."""
    name: str                           # coincide con la clave en producers.yaml
    kind: SegmentKind

    def run(self, ctx: ProducerContext) -> list[str]:
        """Genera segmentos y devuelve los ids creados (status "ready")."""
        ...


# ── Helpers ───────────────────────────────────────────────────────────────────

def segment_audio_path(ctx: ProducerContext, kind: str, seg_id: str) -> Path:
    """Ruta final del audio de un segmento."""
    return ctx.data_dir / "segments" / kind / f"{seg_id}.wav"


def write_segment_audio(
    ctx: ProducerContext,
    *,
    kind: str,
    seg_id: str,
    text: str,
    voice_id: str,
) -> AudioInfo:
    """
    Sintetiza `text` con la voz `voice_id` de forma atómica:
    escribe a un temporal en el mismo directorio y lo renombra con os.replace.
    Nunca deja temporales a medias si el TTS falla.
    """
    final_path = segment_audio_path(ctx, kind, seg_id)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    # El temporal conserva la extensión .wav para los TTS que la inspeccionan
    tmp_path = final_path.with_name(f".{seg_id}.tmp.wav")
    try:
        info = ctx.tts.synthesize(text, voice_id, tmp_path)
        os.replace(tmp_path, final_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return AudioInfo(path=final_path, duration_s=info.duration_s)


def pick_voice(config: RadioConfig, voice_id: str) -> VoiceEntry:
    """
    Devuelve la voz `voice_id` de voices.yaml.
    El consentimiento ya lo valida la configuración; aquí solo se exige que exista.
    """
    for voice in config.voices.voices:
        if voice.id == voice_id:
            return voice
    raise ValueError(f"Voz desconocida (no está en voices.yaml): {voice_id!r}")
