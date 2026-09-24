"""
Infraestructura común de los producers de Radio Parra.

Un producer genera segmentos (guion + audio) y los deja en la BD con
status "ready" para que el scheduler los emita. Este módulo define el
contexto compartido, el protocolo Producer y helpers de escritura atómica
(§3.3: audio en ``data/tmp/`` → ``os.replace`` a ``data/stock/<kind>/`` → fila).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from radio.core.clock import Clock
from radio.core.config import RadioConfig
from radio.core.models import AudioInfo, SegmentKind, Voice
from radio.core.paths import commit_audio, stock_dir, tmp_dir
from radio.core.store import DB
from radio.providers.llm.base import LLM
from radio.providers.tts.base import TTS

# Kind que produce cada productor de producers.yaml (si no está, el propio nombre)
PRODUCER_KINDS: dict[str, SegmentKind] = {
    "time_signal": "time_signal",
    "music_tinydesk": "music",
}


def producer_kind(name: str) -> SegmentKind:
    """Kind de los segmentos que genera el productor ``name``."""
    return PRODUCER_KINDS.get(name, name)


# ── Contexto y protocolo ──────────────────────────────────────────────────────

@dataclass
class ProducerContext:
    """Dependencias que recibe cada producer al ejecutarse."""
    db: DB
    clock: Clock
    llm: LLM
    tts: TTS
    config: RadioConfig
    data_dir: Path                      # el audio va a data_dir/stock/<kind>/<id>.wav
    prompts_dir: Path = Path("prompts")


class Producer(Protocol):
    """Interfaz mínima de un producer."""
    name: str                           # coincide con la clave en producers.yaml
    kind: SegmentKind
    factual: bool

    def run(self, ctx: ProducerContext) -> list[str]:
        """Genera segmentos y devuelve los ids creados (status "ready")."""
        ...


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
    no deja temporales a medias ni toca el stock.
    """
    final_path = segment_audio_path(ctx, kind, seg_id)
    tmp = tmp_dir(ctx.data_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    # El temporal conserva la extensión .wav para los TTS que la inspeccionan
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
