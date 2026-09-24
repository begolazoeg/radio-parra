"""
Emisora de Radio Parra (§4.4): el mundo "siempre encendido" de §2.

- ``engine``: ``StationEngine``, programación con lookahead sobre un reproductor con
  cola y eventos (``play_log``, interrupciones, emergencia, watchdog). Determinista
  con ``FakeClock`` + ``FakeEventBackend``: es lo que ejecuta ``radio simulate``.
- ``queue``: espejo de la cola del reproductor.
- ``service``: el proceso real (``radio station``) con mpv y señales del sistema.

Nunca importa productores, proveedores de LLM/TTS ni código de red (invariante 2).
"""

from radio.station.engine import AiredItem, EngineStats, StationEngine
from radio.station.queue import EMERGENCY_KIND, AirQueue, QueueItem

__all__ = [
    "EMERGENCY_KIND",
    "AiredItem",
    "AirQueue",
    "EngineStats",
    "QueueItem",
    "StationEngine",
]
