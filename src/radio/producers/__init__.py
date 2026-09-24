"""
Producers de Radio Parra: generan segmentos listos para emitir.
"""

from radio.producers.base import (
    Producer,
    ProducerContext,
    pick_voice,
    producer_kind,
    write_segment_audio,
)
from radio.producers.runner import ProducerRunner
from radio.producers.time_signal import TimeSignalProducer

__all__ = [
    "Producer",
    "ProducerContext",
    "ProducerRunner",
    "TimeSignalProducer",
    "pick_voice",
    "producer_kind",
    "write_segment_audio",
]
