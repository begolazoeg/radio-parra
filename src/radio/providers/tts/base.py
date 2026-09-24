"""
Protocolo base para proveedores TTS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from radio.core.models import AudioInfo, Voice


@runtime_checkable
class TTS(Protocol):
    """
    Interfaz mínima que todo proveedor TTS debe implementar (§4.1).
    Escribe el audio de ``text`` con ``voice`` en ``out_path`` (normalmente dentro
    de ``data/tmp/``; el llamador lo mueve después al stock).
    """

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        ...
