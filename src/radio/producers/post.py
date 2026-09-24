"""
Postproducción de audio (§4.2, paso 5 del pipeline).

- ``AudioPost``: protocolo. ``process(audio)`` recibe un audio temporal (en
  ``data/tmp/``) y devuelve el audio procesado: el mismo si no lo toca, o uno nuevo
  en el mismo directorio (el llamador borra el original).
- ``FfmpegLoudnorm``: ``loudnorm`` de ffmpeg en dos pasadas (EBU R128, objetivo
  ``station.loudness_lufs``, ≈ −16 LUFS) y recorte de silencios al principio y al
  final. La duración final se lee con mutagen.
- ``NullPost``: no hace nada (tests, simulación o máquinas sin ffmpeg).
- ``choose_post(config)``: ffmpeg si está en el PATH; si no, ``NullPost`` con aviso.

El ejecutable se invoca con ``subprocess`` (sin shell) a través de un ``runner``
inyectable, de modo que los tests comprueban los argumentos sin tener ffmpeg.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

import mutagen

from radio.core.config import RadioConfig
from radio.core.models import AudioInfo

logger = logging.getLogger(__name__)

# Pico verdadero y rango de loudness objetivo (valores habituales en radio)
TRUE_PEAK_DB = -1.5
LOUDNESS_RANGE = 11.0
# Umbral y duración mínima de silencio que se recorta en los extremos
SILENCE_THRESHOLD_DB = -50.0
SILENCE_MIN_S = 0.2
FFMPEG_TIMEOUT_S = 600.0

# Ejecuta un comando y devuelve (código, stderr)
CommandRunner = Callable[[Sequence[str]], tuple[int, str]]


class PostError(RuntimeError):
    """ffmpeg ha fallado o su salida no es interpretable."""


@runtime_checkable
class AudioPost(Protocol):
    """Postproceso de un audio temporal."""

    def process(self, audio: AudioInfo) -> AudioInfo:
        ...


class NullPost:
    """No toca el audio."""

    def process(self, audio: AudioInfo) -> AudioInfo:
        return audio


def _run_subprocess(args: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(
        list(args),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=FFMPEG_TIMEOUT_S,
        check=False,
    )
    return proc.returncode, proc.stderr


def audio_duration(path: Path) -> float:
    """Duración con mutagen (0.0 si no se puede leer)."""
    try:
        audio = mutagen.File(path)
    except Exception:  # mutagen lanza tipos muy variados
        return 0.0
    if audio is None or getattr(audio, "info", None) is None:
        return 0.0
    return float(getattr(audio.info, "length", 0.0) or 0.0)


class FfmpegLoudnorm:
    """
    Normalización de loudness en dos pasadas + recorte de silencios con ffmpeg.

    1ª pasada: mide (``loudnorm=print_format=json``) sobre el audio ya recortado.
    2ª pasada: aplica los valores medidos (``linear=true``) y escribe
    ``<stem>.post<ext>`` junto al original. WAV sale en PCM 16 bits; el resto con el
    códec por defecto de ffmpeg para su extensión.
    """

    def __init__(
        self,
        target_lufs: float = -16.0,
        *,
        ffmpeg: str = "ffmpeg",
        trim_silence: bool = True,
        runner: CommandRunner | None = None,
    ) -> None:
        self.target_lufs = target_lufs
        self.ffmpeg = ffmpeg
        self.trim_silence = trim_silence
        self.runner: CommandRunner = runner or _run_subprocess

    def _trim_filter(self) -> str:
        if not self.trim_silence:
            return ""
        one_side = (
            f"silenceremove=start_periods=1:start_duration={SILENCE_MIN_S}"
            f":start_threshold={SILENCE_THRESHOLD_DB}dB"
        )
        # Recorta el principio, da la vuelta, recorta (= final) y vuelve a girar
        return f"{one_side},areverse,{one_side},areverse,"

    def _loudnorm(self) -> str:
        return f"loudnorm=I={self.target_lufs}:TP={TRUE_PEAK_DB}:LRA={LOUDNESS_RANGE}"

    def measure_args(self, src: Path) -> list[str]:
        """Argumentos de la pasada de medida."""
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(src),
            "-af", f"{self._trim_filter()}{self._loudnorm()}:print_format=json",
            "-f", "null", "-",
        ]

    def apply_args(self, src: Path, dst: Path, measured: dict[str, str]) -> list[str]:
        """Argumentos de la pasada que escribe el audio normalizado."""
        loudnorm = (
            f"{self._loudnorm()}"
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
        )
        args = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src),
            "-af", f"{self._trim_filter()}{loudnorm}",
        ]
        if dst.suffix.lower() == ".wav":
            args += ["-c:a", "pcm_s16le"]
        return [*args, str(dst)]

    @staticmethod
    def parse_measurement(stderr: str) -> dict[str, str]:
        """Extrae el JSON que ``loudnorm`` imprime al final de stderr."""
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
        if match is None:
            raise PostError("ffmpeg no ha devuelto la medida de loudnorm")
        data = json.loads(match.group(0))
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        missing = [k for k in keys if k not in data]
        if missing:
            raise PostError(f"medida de loudnorm incompleta: faltan {missing}")
        return {k: str(data[k]) for k in keys}

    def process(self, audio: AudioInfo) -> AudioInfo:
        src = audio.path
        dst = src.with_name(f"{src.stem}.post{src.suffix}")
        code, stderr = self.runner(self.measure_args(src))
        if code != 0:
            raise PostError(f"ffmpeg (medida) salió con {code}: {stderr[-500:]}")
        measured = self.parse_measurement(stderr)
        try:
            code, stderr = self.runner(self.apply_args(src, dst, measured))
            if code != 0:
                raise PostError(f"ffmpeg (normalización) salió con {code}: {stderr[-500:]}")
            duration = audio_duration(dst)
            if duration <= 0:
                raise PostError(f"ffmpeg ha producido un audio vacío: {dst}")
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
        return AudioInfo(path=dst, duration_s=duration)


def choose_post(config: RadioConfig, *, which: Callable[[str], str | None] = shutil.which) -> AudioPost:
    """ffmpeg loudnorm si ffmpeg está instalado; si no, ``NullPost`` con un aviso."""
    ffmpeg = which("ffmpeg")
    if ffmpeg is None:
        logger.warning(
            "ffmpeg no está instalado: el audio se registra sin normalizar (loudnorm)"
        )
        return NullPost()
    return FfmpegLoudnorm(config.station.loudness_lufs, ffmpeg=ffmpeg)
