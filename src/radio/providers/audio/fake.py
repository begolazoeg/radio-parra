"""
Backend de audio falso *con eventos* para simulación y tests deterministas.

Reproduce el modelo de ``MpvIpcBackend`` (cola FIFO, ``Started``/``Ended``,
``queued()``, ``current()``, ``skip()``, watchdog) sin procesos, hilos ni tiempo real:
el tiempo solo avanza cuando alguien llama a ``finish()`` (o a ``play()``, que
termina todo lo que haya hasta ese archivo).

Con ``duration_of`` y ``advance`` (p. ej. ``FakeClock.advance``), cada archivo que
termina con ``eof`` avanza el reloj su duración, de modo que las marcas de tiempo de
los eventos quedan en tiempo simulado::

    clock = FakeClock(start)
    audio = FakeEventBackend(clock, duration_of=lambda p: durations[p], advance=clock.advance)
    audio.add_listener(on_event)
    audio.enqueue(a); audio.enqueue(b)   # Started(a)
    audio.finish()                       # Ended(a, eof) + Started(b), reloj += dur(a)
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Callable
from pathlib import Path

from radio.core.clock import Clock
from radio.providers.audio.events import (
    Ended,
    EndReason,
    EventListener,
    ListenerSet,
    PlayerEvent,
    Started,
)


class FakeEventBackend:
    """
    Backend determinista con la misma API de eventos que ``MpvIpcBackend``.

    - ``enqueue(path)``: al final de la cola; si no suena nada, empieza (``Started``).
    - ``finish(reason="eof")``: termina el archivo en curso y empieza el siguiente.
    - ``skip()``: ``finish("skipped")`` si hay algo sonando.
    - ``play(path)``: encola y termina (``eof``) todo hasta ese archivo, incluido.
    - ``crash()``: simula que el reproductor muere y el watchdog lo relanza: el archivo
      en curso termina con ``error``, ``restarts`` sube y sigue el siguiente pendiente.
    - ``calls``: registro de llamadas (``play``/``enqueue``/``skip``) como ``NullAudioBackend``.

    Los oyentes se llaman de forma síncrona, en el mismo hilo, en orden.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        duration_of: Callable[[Path], float] | None = None,
        advance: Callable[[float], None] | None = None,
    ) -> None:
        self._clock = clock
        self._duration_of = duration_of
        self._advance = advance
        self._listeners = ListenerSet()
        self._pending: deque[tuple[int, Path]] = deque()
        self._current: tuple[int, Path] | None = None
        self._tokens = itertools.count()
        self._restarts = 0
        self._closed = False
        self.calls: list[dict[str, object]] = []

    # ── AudioBackend ─────────────────────────────────────────────────────────

    def enqueue(self, path: Path) -> None:
        self.calls.append({"action": "enqueue", "path": path})
        if self._closed:
            return
        self._add(path)

    def play(self, path: Path) -> None:
        self.calls.append({"action": "play", "path": path})
        if self._closed:
            return
        token = self._add(path)
        # Termina (eof) todo lo que haya delante y el propio archivo
        while self._current is not None and (
            self._current[0] == token or any(t == token for t, _ in self._pending)
        ):
            self.finish()

    def skip(self) -> None:
        self.calls.append({"action": "skip"})
        if self._current is not None:
            self.finish("skipped")

    # ── Control de la simulación ─────────────────────────────────────────────

    def finish(self, reason: EndReason = "eof") -> None:
        """Termina el archivo en curso con ``reason`` y empieza el siguiente si lo hay."""
        if self._current is None:
            return
        _, path = self._current
        if reason == "eof" and self._duration_of is not None and self._advance is not None:
            self._advance(self._duration_of(path))
        self._current = None
        events: list[PlayerEvent] = [Ended(path, self._clock.now(), reason)]
        events += self._start_next()
        self._emit(events)

    def crash(self) -> None:
        """Simula una caída del reproductor con relanzamiento inmediato."""
        self._restarts += 1
        if self._current is not None:
            self.finish("error")
        elif self._pending:
            _, path = self._pending.popleft()
            events: list[PlayerEvent] = [Ended(path, self._clock.now(), "error")]
            events += self._start_next()
            self._emit(events)

    # ── API de eventos / estado ──────────────────────────────────────────────

    @property
    def restarts(self) -> int:
        return self._restarts

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: EventListener) -> None:
        self._listeners.remove(listener)

    def queued(self) -> int:
        return len(self._pending)

    def current(self) -> Path | None:
        return self._current[1] if self._current is not None else None

    def alive(self) -> bool:
        return not self._closed

    def close(self) -> None:
        """Como en mpv: el archivo en curso termina ``skipped``; los pendientes se descartan."""
        if self._closed:
            return
        self._closed = True
        if self._current is not None:
            (_, path), self._current = self._current, None
            self._emit([Ended(path, self._clock.now(), "skipped")])
        self._pending.clear()

    # ── Internos ─────────────────────────────────────────────────────────────

    def _add(self, path: Path) -> int:
        token = next(self._tokens)
        self._pending.append((token, path))
        if self._current is None:
            self._emit(self._start_next())
        return token

    def _start_next(self) -> list[PlayerEvent]:
        if self._current is not None or not self._pending:
            return []
        self._current = self._pending.popleft()
        return [Started(self._current[1], self._clock.now())]

    def _emit(self, events: list[PlayerEvent]) -> None:
        self._listeners.emit(events)
