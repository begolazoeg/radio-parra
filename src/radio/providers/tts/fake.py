"""
Implementación fake del proveedor TTS para tests.
Genera un archivo WAV de silencio con duración proporcional al len(text).
Usa solo stdlib (wave + struct), sin dependencias de audio externas.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import Any

from radio.core.models import AudioInfo

# Tasa de muestreo y factor de conversión texto → duración
_SAMPLE_RATE = 22050
_CHARS_PER_SECOND = 15.0  # velocidad de lectura aproximada


class FakeTTS:
    """
    TTS falso para tests.
    Genera silencio WAV de duración proporcional al número de caracteres.
    """

    def __init__(self, chars_per_second: float = _CHARS_PER_SECOND) -> None:
        self.chars_per_second = chars_per_second
        self.calls: list[dict[str, Any]] = []

    def synthesize(
        self,
        text: str,
        voice: str,
        out_path: Path,
    ) -> AudioInfo:
        self.calls.append({"text": text, "voice": voice, "out_path": out_path})

        duration_s = max(0.1, len(text) / self.chars_per_second)
        num_frames = int(_SAMPLE_RATE * duration_s)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)          # 16 bits
            wf.setframerate(_SAMPLE_RATE)
            # Escribe frames de silencio (valor 0)
            silence = struct.pack("<h", 0) * num_frames
            wf.writeframes(silence)

        return AudioInfo(path=out_path, duration_s=duration_s)
