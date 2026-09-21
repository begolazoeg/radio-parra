"""
Protocolo base para proveedores TTS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from radio.core.models import AudioInfo


@runtime_checkable
class TTS(Protocol):
    """Interfaz mínima que todo proveedor TTS debe implementar."""

    def synthesize(
        self,
        text: str,
        voice: str,
        out_path: Path,
    ) -> AudioInfo:
        ...
