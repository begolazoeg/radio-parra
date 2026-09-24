"""
Registro de productores: nombre en producers.yaml → fábrica.

Cada fábrica recibe la ``RadioConfig`` y devuelve el productor configurado. Construir
un productor no tiene efectos (ni red ni disco): ``radio stock`` lo hace solo para
leer su ``kind`` y su ``target_stock``. Un productor nuevo se añade aquí.
"""

from __future__ import annotations

from collections.abc import Callable

from radio.core.config import RadioConfig
from radio.producers.base import Producer
from radio.producers.host_intro import HostIntroProducer
from radio.producers.music_tinydesk import MusicTinyDeskProducer
from radio.producers.time_signal import TimeSignalProducer

ProducerFactory = Callable[[RadioConfig], Producer]

PRODUCERS: dict[str, ProducerFactory] = {
    TimeSignalProducer.name: TimeSignalProducer,
    MusicTinyDeskProducer.name: MusicTinyDeskProducer,
    HostIntroProducer.name: HostIntroProducer,
}


def build_producer(name: str, config: RadioConfig) -> Producer:
    """Productor ``name`` configurado; KeyError con los disponibles si no existe."""
    try:
        factory = PRODUCERS[name]
    except KeyError:
        raise KeyError(
            f"Productor desconocido {name!r} (disponibles: {', '.join(sorted(PRODUCERS))})"
        ) from None
    return factory(config)


def build_producers(config: RadioConfig, *, only_active: bool = True) -> list[Producer]:
    """Productores de producers.yaml que existen en el registro (por defecto, activos)."""
    out: list[Producer] = []
    for name, settings in config.producers.producers.items():
        if name in PRODUCERS and (settings.active or not only_active):
            out.append(build_producer(name, config))
    return out
