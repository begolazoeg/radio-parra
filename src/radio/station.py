"""
Bucle real de la emisora (``radio station``).

Monta las piezas de producción —``SystemClock`` en la zona de la emisora, BD en
``data/state.db``, ``MpvAudioBackend``, ``Scheduler`` con la parrilla y un
``ProducerRunner`` con los producers de producers.yaml— y emite sin parar.

Decisiones de Fase 1
--------------------
- Los producers se ejecutan *entre segmentos*, en el mismo hilo: antes de cada
  ``Playout.step()`` se llama a ``runner.tick()``. Un producer lento retrasa el
  siguiente segmento; moverlos a un hilo en segundo plano es trabajo futuro.
- Los proveedores salen de ``radio.providers.registry`` (solo "fake" por ahora). Si
  el LLM o el TTS configurado no está disponible, se avisa y la emisora arranca en
  modo solo música (sin producers).
- SIGINT/SIGTERM: se pide parar, se corta el audio en curso y se cierra la BD.
- Si un paso no emite nada (ni siquiera audio de emergencia), se espera
  ``IDLE_SLEEP_S`` segundos antes de volver a intentarlo.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from radio.core.clock import Clock, SystemClock
from radio.core.config import RadioConfig
from radio.core.paths import db_path
from radio.core.playout import Playout
from radio.core.scheduler import Scheduler
from radio.core.store import DB
from radio.producers import (
    Producer,
    ProducerContext,
    ProducerRunner,
    TimeSignalProducer,
)
from radio.providers.audio.mpv import MpvAudioBackend
from radio.providers.registry import ProviderNotAvailable, build_llm, build_tts

logger = logging.getLogger(__name__)

IDLE_SLEEP_S = 5.0


def default_producers() -> list[Producer]:
    """Producers implementados; el runner solo ejecuta los activos en producers.yaml."""
    return [TimeSignalProducer()]


def run_station_loop(
    playout: Playout,
    runner: ProducerRunner | None,
    *,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], object],
    idle_s: float = IDLE_SLEEP_S,
) -> int:
    """
    Bucle principal: producers → un segmento → repetir, hasta que ``should_stop()``.
    Devuelve el número de segmentos emitidos. Las dependencias se inyectan para
    poder probarlo con ``NullAudioBackend`` y ``FakeClock``.
    """
    aired = 0
    while not should_stop():
        if runner is not None:
            runner.tick()
            if should_stop():
                break
        outcome = playout.step()
        if outcome is not None:
            aired += 1
            continue
        if playout.last_emergency is None:
            logger.warning("Nada que emitir; reintento en %.0f s", idle_s)
            sleep(idle_s)
    return aired


def build_runner(
    config: RadioConfig, db: DB, clock: Clock, data_dir: Path, prompts_dir: Path
) -> ProducerRunner | None:
    """ProducerRunner con los proveedores configurados, o None (modo solo música)."""
    providers = config.station.providers
    try:
        llm = build_llm(providers.get("llm"))
        tts = build_tts(providers.get("tts"))
    except ProviderNotAvailable as exc:
        logger.warning("%s — la emisora arranca en modo solo música (sin producers)", exc)
        return None
    ctx = ProducerContext(
        db=db,
        clock=clock,
        llm=llm,
        tts=tts,
        config=config,
        data_dir=data_dir,
        prompts_dir=prompts_dir,
    )
    return ProducerRunner(ctx, default_producers())


def run_station(
    *,
    config_dir: Path,
    data_dir: Path,
    prompts_dir: Path = Path("prompts"),
    emergency_dir: Path | None = Path("assets/emergency"),
) -> int:
    """Arranca la emisora real hasta recibir SIGINT/SIGTERM. Devuelve segmentos emitidos."""
    config = RadioConfig.load(config_dir)
    tz = config.station.timezone
    data_dir.mkdir(parents=True, exist_ok=True)
    db = DB(db_path(data_dir))
    clock = SystemClock(tz)
    playout = Playout(
        db,
        Scheduler(config.grid),
        MpvAudioBackend(),
        clock,
        tz=tz,
        emergency_dir=emergency_dir,
    )
    runner = build_runner(config, db, clock, data_dir, prompts_dir)
    stop = threading.Event()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Señal %s recibida: parando la emisora", signal.Signals(signum).name)
        stop.set()
        # skip() toma un lock del backend: se hace fuera del manejador para no bloquearlo
        threading.Thread(target=playout.interrupt, daemon=True).start()

    previous = {
        sig: signal.signal(sig, handle_signal) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    logger.info(
        "%s en antena (zona %s, BD %s)", config.station.name, tz, db_path(data_dir)
    )
    try:
        return run_station_loop(playout, runner, should_stop=stop.is_set, sleep=stop.wait)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        db.close()
        logger.info("Emisora parada; BD cerrada")
