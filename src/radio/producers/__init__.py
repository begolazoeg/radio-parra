"""
Productores de Radio Parra: generan segmentos listos para emitir (§4.2).
"""

from radio.producers.base import (
    Draft,
    DraftRejected,
    Producer,
    ProducerContext,
    ProducerError,
    RunStats,
    StagedProducer,
    budget_exhausted,
    call_llm,
    pick_voice,
    write_segment_audio,
)
from radio.producers.host_intro import HostIntroProducer
from radio.producers.music_tinydesk import MusicTinyDeskProducer
from radio.producers.post import AudioPost, FfmpegLoudnorm, NullPost, choose_post
from radio.producers.registry import PRODUCERS, build_producer, build_producers
from radio.producers.runner import (
    ProduceReport,
    RunResult,
    build_context,
    produce,
    run_producer,
)
from radio.producers.time_signal import TimeSignalProducer

__all__ = [
    "PRODUCERS",
    "AudioPost",
    "Draft",
    "DraftRejected",
    "FfmpegLoudnorm",
    "HostIntroProducer",
    "MusicTinyDeskProducer",
    "NullPost",
    "ProduceReport",
    "Producer",
    "ProducerContext",
    "ProducerError",
    "RunResult",
    "RunStats",
    "StagedProducer",
    "TimeSignalProducer",
    "budget_exhausted",
    "build_context",
    "build_producer",
    "build_producers",
    "call_llm",
    "choose_post",
    "pick_voice",
    "produce",
    "run_producer",
    "write_segment_audio",
]
