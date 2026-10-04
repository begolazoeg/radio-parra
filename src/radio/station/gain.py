"""
Normalización de volumen **en reproducción** (§12 Fase 2, "Normalización de volumen").

La palabra (señal horaria, intros) se normaliza al producirla (``post``: ``loudnorm``
a ``station.loudness_lufs``). La música de Tiny Desk **no se puede modificar ni
recodificar** (términos de NPR, ADR 0002), así que se iguala al reproducir: el
productor solo mide el archivo (``meta.loudness_lufs`` y ``meta.true_peak_db``) y la
emisora pide al reproductor una ganancia para *ese* archivo (en mpv, un filtro de
volumen por archivo que no afecta al siguiente). El archivo en disco no cambia.

Cálculo (``playback_gain_db``):

1. ``objetivo − medido`` (dB), p. ej. −16 − (−21) = +5 dB.
2. Acotado a ``[gain_min_db, gain_max_db]`` (por defecto −12..+6 dB).
3. Limitado para que ``true_peak_db + ganancia ≤ true_peak_ceiling_db`` (−1 dBTP):
   no se sube lo que ya roza el 0 dBFS (sin limitador, subir recortaría). Si el
   archivo ya pasa del techo, la ganancia puede quedar negativa.
4. Sin medida (``None``, no finita) o con ``normalize: false`` → 0 dB. Es el caso de la
   palabra (ya normalizada) y del bucle de emergencia.

Función pura: la emisora no mide nada (invariante 2), solo lee ``meta``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from radio.core.config import RadioConfig
from radio.core.models import Segment

# Por debajo de esto una ganancia no se nota: se envía 0 dB
GAIN_EPSILON_DB = 0.05


@dataclass(frozen=True)
class GainPolicy:
    """Parámetros de la normalización en reproducción (``station.yaml → audio``)."""
    enabled: bool = True
    target_lufs: float = -16.0
    min_db: float = -12.0
    max_db: float = 6.0
    peak_ceiling_db: float = -1.0

    @classmethod
    def from_config(cls, config: RadioConfig) -> GainPolicy:
        audio = config.station.audio
        return cls(
            enabled=audio.normalize,
            target_lufs=config.station.loudness_lufs,
            min_db=audio.gain_min_db,
            max_db=audio.gain_max_db,
            peak_ceiling_db=audio.true_peak_ceiling_db,
        )


DISABLED = GainPolicy(enabled=False)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def playback_gain_db(
    loudness_lufs: float | None, true_peak_db: float | None, policy: GainPolicy
) -> float:
    """Ganancia (dB) para un archivo con esa medida (ver docstring del módulo)."""
    lufs = _number(loudness_lufs)
    if not policy.enabled or lufs is None:
        return 0.0
    gain = min(max(policy.target_lufs - lufs, policy.min_db), policy.max_db)
    peak = _number(true_peak_db)
    if peak is not None:
        gain = min(gain, policy.peak_ceiling_db - peak)
    gain = max(gain, policy.min_db)
    return 0.0 if abs(gain) < GAIN_EPSILON_DB else round(gain, 2)


def segment_gain_db(seg: Segment | None, policy: GainPolicy) -> float:
    """Ganancia para un segmento según ``meta`` (0 dB sin segmento o sin medida)."""
    if seg is None:
        return 0.0
    return playback_gain_db(seg.meta.get("loudness_lufs"), seg.meta.get("true_peak_db"), policy)


def has_measurement(seg: Segment | None) -> bool:
    """True si el segmento trae ``meta.loudness_lufs`` utilizable."""
    return seg is not None and _number(seg.meta.get("loudness_lufs")) is not None
