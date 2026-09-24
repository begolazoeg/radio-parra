"""
Protocolos base para backends de reproducción de audio.

- ``AudioBackend``: interfaz mínima de ARCHITECTURE.md §4.1 (play/enqueue/skip).
- ``QueueingAudioBackend``: lo que además necesita la emisora para mantener un
  *lookahead* de 2–3 unidades y escribir ``play_log`` al empezar y terminar (§4.4):
  eventos ``Started``/``Ended``, tamaño de la cola, archivo en curso, vaciado de lo
  pendiente (interrupciones), watchdog y cierre.

Ganancia por archivo: ``QueueingAudioBackend.enqueue(path, gain_db=...)`` acepta una
ganancia en dB que se aplica **solo a ese archivo** (normalización en reproducción,
``radio.station.gain``); el archivo en disco no se toca. Es compatible con la interfaz
mínima: ``enqueue(path)`` sigue valiendo (0 dB).

Implementaciones: ``MpvIpcBackend`` (real), ``FakeEventBackend`` (simulación
determinista con eventos) y ``NullAudioBackend`` (solo registra llamadas).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from radio.providers.audio.events import EventListener


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


@runtime_checkable
class QueueingAudioBackend(AudioBackend, Protocol):
    """
    Backend con cola propia y eventos de reproducción.

    Semántica común:
    - ``enqueue(path, gain_db=0.0)``: añade al final de la cola; si no suena nada,
      empieza ya. ``gain_db`` se aplica solo a ese archivo (el siguiente vuelve a 0 dB
      salvo que traiga la suya) y se conserva si el watchdog relanza el reproductor.
    - ``play(path)``: ``enqueue(path)`` + bloquear hasta que *ese* archivo termine
      (por fin, salto o error). Si hay cosas delante en la cola, suenan antes.
    - ``skip()``: corta el archivo en curso (``Ended(reason="skipped")``) y pasa al
      siguiente; sin nada en curso no hace nada.
    """

    def enqueue(self, path: Path, *, gain_db: float = 0.0) -> None:
        """Encola ``path`` con una ganancia propia (dB) que no afecta a otros archivos."""
        ...

    @property
    def restarts(self) -> int:
        """Veces que el watchdog ha tenido que relanzar el reproductor."""
        ...

    def add_listener(self, listener: EventListener) -> None:
        """Suscribe ``listener`` a los eventos ``Started``/``Ended``."""
        ...

    def remove_listener(self, listener: EventListener) -> None:
        """Da de baja un oyente (no falla si no estaba)."""
        ...

    def queued(self) -> int:
        """Archivos pendientes *después* del que suena ahora."""
        ...

    def current(self) -> Path | None:
        """Archivo que suena ahora, o ``None``."""
        ...

    def clear_pending(self) -> int:
        """
        Descarta los archivos encolados que aún no han empezado (sin eventos) y
        devuelve cuántos eran. El que suena sigue sonando. Lo usa la emisora para
        dar paso a una interrupción (señal horaria) por delante de su *lookahead*.
        """
        ...

    def alive(self) -> bool:
        """True si el reproductor está vivo y conectado."""
        ...

    def close(self) -> None:
        """Para la reproducción y libera recursos. Idempotente."""
        ...
