"""
Protocolo base para backends de reproducción de audio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioBackend(Protocol):
    """Interfaz mínima para controlar la reproducción de audio."""

    def play(self, path: Path) -> None:
        """Reproduce el archivo de forma bloqueante."""
        ...

    def enqueue(self, path: Path) -> None:
        """Encola el archivo para reproducción."""
        ...

    def skip(self) -> None:
        """Salta al siguiente elemento de la cola."""
        ...
