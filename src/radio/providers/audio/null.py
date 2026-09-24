"""
Backend de audio nulo para tests y simulación sin hardware.
Registra todas las llamadas pero no reproduce nada.

No emite eventos: para simular la cola con ``Started``/``Ended`` de forma
determinista está ``FakeEventBackend`` (``radio.providers.audio.fake``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class NullAudioBackend:
    """
    Backend de audio que no hace nada de verdad.
    Registra play/enqueue/skip en self.calls para aserciones en tests.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def play(self, path: Path) -> None:
        self.calls.append({"action": "play", "path": path})

    def enqueue(self, path: Path) -> None:
        self.calls.append({"action": "enqueue", "path": path})

    def skip(self) -> None:
        self.calls.append({"action": "skip"})
