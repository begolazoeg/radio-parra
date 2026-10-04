"""
Emisora de Radio Parra (§4.4): el mundo "siempre encendido" de §2.

- ``engine``: ``StationEngine``, programación con lookahead sobre un reproductor con
  cola y eventos (``play_log``, interrupciones, emergencia, watchdog). Determinista
  con ``FakeClock`` + ``FakeEventBackend``: es lo que ejecuta ``radio simulate``.
- ``queue``: espejo de la cola del reproductor.
- ``gain``: ganancia de reproducción por archivo (normalización sin tocar el audio).
- ``service``: el proceso real (``radio station``) con mpv y señales del sistema.

Nunca importa productores, proveedores de LLM/TTS ni código de red (invariante 2).
"""

from radio.station.engine import AiredItem, EngineStats, GainStats, StationEngine
from radio.station.gain import GainPolicy, playback_gain_db, segment_gain_db
from radio.station.queue import EMERGENCY_KIND, AirQueue, QueueItem

__all__ = [
    "EMERGENCY_KIND",
    "AiredItem",
    "AirQueue",
    "EngineStats",
    "GainPolicy",
    "GainStats",
    "QueueItem",
    "StationEngine",
    "playback_gain_db",
    "segment_gain_db",
]
