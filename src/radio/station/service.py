"""
Proceso real de la emisora (``radio station``, §4.4): mpv + reloj del sistema.

Monta ``StationEngine`` sobre ``MpvIpcBackend`` (argumentos de ``station.yaml →
audio``) y lo conduce desde el hilo principal:

- Los eventos de mpv llegan por el hilo lector del backend a la bandeja del motor,
  que despierta al hilo principal (``on_wake``); este llama a ``drain()``/``tick()``.
- Entre eventos se duerme hasta ``next_wakeup()`` (temporizador de interrupciones o
  reintento), como mucho ``POLL_S`` segundos para detectar relanzamientos de mpv.
- SIGINT/SIGTERM: se deja de programar, se cierra el backend (lo que suena termina
  como ``skipped`` y se registra en ``play_log``) y se cierra la BD.

Sin productores, LLM, TTS ni red (invariante 2): eso es ``radio produce`` (timer).
"""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from types import FrameType

from radio.core.clock import SystemClock
from radio.core.config import RadioConfig
from radio.core.paths import db_path
from radio.core.store import DB
from radio.providers.audio.mpv import MpvError, MpvIpcBackend
from radio.station.engine import StationEngine

logger = logging.getLogger(__name__)

# Espera máxima entre dos pasadas del bucle principal (s)
POLL_S = 1.0


def run_station(
    *,
    config_dir: Path,
    data_dir: Path | None = None,
    emergency_dir: Path | None = None,
    mode: str = "default",
) -> int:
    """
    Arranca la emisora hasta recibir SIGINT/SIGTERM. Devuelve 0 si paró limpia y 1
    si mpv no pudo arrancar (systemd la relanzará).
    """
    config = RadioConfig.load(config_dir)
    tz = config.station.timezone
    data = data_dir or config.data_dir
    data.mkdir(parents=True, exist_ok=True)
    db = DB(db_path(data))
    clock = SystemClock(tz)
    audio = config.station.audio
    backend = MpvIpcBackend(audio.mpv_bin, extra_args=audio.mpv_args, clock=clock)
    wake = threading.Event()
    stop = threading.Event()
    extra = {} if emergency_dir is None else {"emergency_dir": emergency_dir}
    engine = StationEngine.from_config(
        config, db, backend, clock, mode=mode, auto_drain=False, on_wake=wake.set, **extra
    )

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Señal %s recibida: parando la emisora", signal.Signals(signum).name)
        stop.set()
        wake.set()

    previous = {sig: signal.signal(sig, handle_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    logger.info("%s en antena (zona %s, BD %s)", config.station.name, tz, db_path(data))
    code = 0
    try:
        engine.start()
        while not stop.is_set():
            engine.tick()
            wakeup = engine.next_wakeup()
            timeout = POLL_S
            if wakeup is not None:
                timeout = min(POLL_S, max(0.0, (wakeup - clock.now()).total_seconds()))
            wake.wait(timeout)
            wake.clear()
    except MpvError as exc:
        logger.error("No se puede reproducir audio: %s", exc)
        code = 1
    finally:
        engine.stop()
        backend.close()
        engine.drain()          # registra el Ended del cierre
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        db.close()
        logger.info("Emisora parada; BD cerrada")
    return code
