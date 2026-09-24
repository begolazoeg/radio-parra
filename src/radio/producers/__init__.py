"""
Producers de Radio Parra: generan segmentos listos para emitir.
"""

from radio.producers.base import (
    Producer,
    ProducerContext,
    pick_voice,
    write_segment_audio,
)
from radio.producers.host_intro import HostIntroProducer
from radio.producers.runner import ProducerRunner
from radio.producers.time_signal import TimeSignalProducer

__all__ = [
    "HostIntroProducer",
    "Producer",
    "ProducerContext",
    "ProducerRunner",
    "TimeSignalProducer",
    "pick_voice",
    "write_segment_audio",
]
