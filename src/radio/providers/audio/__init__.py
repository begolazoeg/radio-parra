"""
Backends de reproducción de audio (ARCHITECTURE.md §4.1 y §4.4).

- ``MpvIpcBackend``: mpv real por JSON-IPC, con cola, eventos y watchdog.
- ``FakeEventBackend``: misma API de eventos, determinista (simulación y tests).
- ``NullAudioBackend``: solo registra llamadas.
"""

from radio.providers.audio.base import AudioBackend, QueueingAudioBackend
from radio.providers.audio.events import (
    Ended,
    EndReason,
    EventListener,
    EventRecorder,
    PlayerEvent,
    Started,
)
from radio.providers.audio.fake import FakeEventBackend
from radio.providers.audio.mpv import MpvError, MpvIpcBackend
from radio.providers.audio.null import NullAudioBackend

__all__ = [
    "AudioBackend",
    "EndReason",
    "Ended",
    "EventListener",
    "EventRecorder",
    "FakeEventBackend",
    "MpvError",
    "MpvIpcBackend",
    "NullAudioBackend",
    "PlayerEvent",
    "QueueingAudioBackend",
    "Started",
]
