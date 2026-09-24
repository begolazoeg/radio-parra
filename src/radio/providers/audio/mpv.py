"""
Backend de audio real basado en mpv (subproceso por archivo).
Pensado para la Raspberry Pi: sin vídeo, sin terminal, normalización opcional.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)


class MpvAudioBackend:
    """
    Reproduce cada archivo con un proceso `mpv` independiente.
    - play(): bloquea hasta que termina el archivo (o hasta skip()).
    - enqueue(): añade a una cola que se vacía con drain().
    - skip(): corta el archivo en curso (desde otro hilo).
    """

    def __init__(self, mpv_bin: str = "mpv", extra_args: list[str] | None = None) -> None:
        self.mpv_bin = mpv_bin
        self.extra_args = extra_args or []
        self._queue: deque[Path] = deque()
        self._current: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _cmd(self, path: Path) -> list[str]:
        return [
            self.mpv_bin,
            "--no-video",
            "--no-terminal",
            "--really-quiet",
            *self.extra_args,
            str(path),
        ]

    def play(self, path: Path) -> None:
        """Reproduce `path` de forma bloqueante. Un error de mpv se registra, no se propaga."""
        with self._lock:
            self._current = subprocess.Popen(
                self._cmd(path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            proc = self._current
        code = proc.wait()
        with self._lock:
            self._current = None
        if code not in (0, -15, 4):  # 4 = salida por señal/quit en mpv
            log.warning("mpv terminó con código %s para %s", code, path)

    def enqueue(self, path: Path) -> None:
        self._queue.append(path)

    def drain(self) -> None:
        """Reproduce en orden todo lo encolado."""
        while self._queue:
            self.play(self._queue.popleft())

    def skip(self) -> None:
        with self._lock:
            if self._current is not None and self._current.poll() is None:
                self._current.terminate()
