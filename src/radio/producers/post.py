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

Análisis de loudness **sin recodificar** (normalización en reproducción):

- ``AudioAnalyzer``: protocolo. ``analyze(path)`` mide el loudness integrado (LUFS) y
  el pico verdadero (dBTP) de un archivo sin tocarlo. Lo usa ``music_tinydesk``: los
  términos de NPR no permiten modificar el audio (``loudnorm: false``), así que la
  emisora iguala el volumen al reproducir con una ganancia por archivo
  (``radio.station.gain``) calculada a partir de esta medida.
- ``FfmpegLoudnessAnalyzer``: una pasada de ``ffmpeg -af ebur128=peak=true`` (o
  ``loudnorm=print_format=json``) con salida a ``-f null``; se lee el resumen de stderr.
- ``NullAnalyzer``: no mide (devuelve ``None``: sin medida → ganancia 0 dB).
- ``choose_analyzer()``: ffmpeg si está en el PATH; si no, ``NullAnalyzer``.
- ``analyze_stock(db, analyzer)``: mide el stock ya registrado (``radio analyze-loudness``).

El ejecutable se invoca con ``subprocess`` (sin shell) a través de un ``runner``
inyectable, de modo que los tests comprueban los argumentos sin tener ffmpeg.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import mutagen

from radio.core.config import RadioConfig
from radio.core.models import AudioInfo
from radio.core.store import DB

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


# ── Análisis de loudness (sin recodificar) ────────────────────────────────────

AnalysisMethod = Literal["ebur128", "loudnorm"]

# Resumen final de ``ebur128``: el último bloque "Summary:" de stderr
_EBUR128_I_RE = re.compile(
    r"Integrated loudness:\s*I:\s*(?P<i>-?(?:inf|nan|\d+(?:\.\d+)?))\s*LUFS", re.IGNORECASE
)
_EBUR128_TP_RE = re.compile(
    r"True peak:\s*Peak:\s*(?P<tp>-?(?:inf|nan|\d+(?:\.\d+)?))\s*dBFS", re.IGNORECASE
)
# Por debajo de esto el archivo es prácticamente silencio: la medida no sirve
MIN_MEASURABLE_LUFS = -70.0


@dataclass(frozen=True)
class LoudnessMeasurement:
    """Loudness integrado (LUFS) y pico verdadero (dBTP; ``None`` si no se midió)."""
    integrated_lufs: float
    true_peak_db: float | None = None


def _finite(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_ebur128_summary(stderr: str) -> LoudnessMeasurement:
    """
    Extrae I (LUFS) y el pico verdadero del resumen que ``ebur128`` imprime al final
    de stderr. Solo se mira lo que sigue al último ``Summary:`` (las líneas por trama,
    si las hay, no cuentan). ``PostError`` si no hay resumen o el loudness no es finito.
    """
    idx = stderr.rfind("Summary:")
    if idx < 0:
        raise PostError("ffmpeg no ha devuelto el resumen de ebur128")
    summary = stderr[idx:]
    m_i = _EBUR128_I_RE.search(summary)
    if m_i is None:
        raise PostError("resumen de ebur128 sin loudness integrado")
    lufs = _finite(m_i.group("i"))
    if lufs is None or lufs <= MIN_MEASURABLE_LUFS:
        raise PostError(f"loudness integrado no medible: {m_i.group('i')} LUFS")
    m_tp = _EBUR128_TP_RE.search(summary)
    peak = _finite(m_tp.group("tp")) if m_tp is not None else None
    return LoudnessMeasurement(lufs, peak)


def parse_loudnorm_summary(stderr: str) -> LoudnessMeasurement:
    """Igual que ``parse_ebur128_summary`` pero con el JSON de ``loudnorm``."""
    measured = FfmpegLoudnorm.parse_measurement(stderr)
    lufs = _finite(measured["input_i"])
    if lufs is None or lufs <= MIN_MEASURABLE_LUFS:
        raise PostError(f"loudness integrado no medible: {measured['input_i']} LUFS")
    return LoudnessMeasurement(lufs, _finite(measured["input_tp"]))


def parse_loudness(stderr: str) -> LoudnessMeasurement:
    """Detecta el formato (resumen de ``ebur128`` o JSON de ``loudnorm``) y lo interpreta."""
    if "Summary:" in stderr:
        return parse_ebur128_summary(stderr)
    return parse_loudnorm_summary(stderr)


@runtime_checkable
class AudioAnalyzer(Protocol):
    """Mide el loudness de un archivo sin modificarlo."""

    def analyze(self, path: Path) -> LoudnessMeasurement | None:
        """Medida del archivo; ``None`` si este analizador no mide. ``PostError`` si falla."""
        ...


class NullAnalyzer:
    """No mide nada (máquinas sin ffmpeg, tests): la ganancia en antena será 0 dB."""

    def analyze(self, path: Path) -> LoudnessMeasurement | None:
        return None


class FfmpegLoudnessAnalyzer:
    """
    Una sola pasada de ffmpeg de solo lectura (``-f null -``: no escribe audio).

    - ``ebur128`` (por defecto): ``-af ebur128=peak=true:framelog=verbose``. Con
      ``framelog=verbose`` las líneas por trama (10 por segundo: miles en un concierto)
      no salen con el nivel de log por defecto; solo el resumen.
    - ``loudnorm``: ``-af loudnorm=print_format=json`` (la misma medida que la primera
      pasada de ``FfmpegLoudnorm``). Alternativa si el ffmpeg de la máquina no acepta
      ``framelog``.
    """

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        method: AnalysisMethod = "ebur128",
        runner: CommandRunner | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.method: AnalysisMethod = method
        self.runner: CommandRunner = runner or _run_subprocess

    def args(self, path: Path) -> list[str]:
        """Argumentos de la pasada de medida."""
        if self.method == "loudnorm":
            filt = f"loudnorm=I=-16:TP={TRUE_PEAK_DB}:LRA={LOUDNESS_RANGE}:print_format=json"
        else:
            filt = "ebur128=peak=true:framelog=verbose"
        return [
            self.ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
            "-vn", "-af", filt, "-f", "null", "-",
        ]

    def analyze(self, path: Path) -> LoudnessMeasurement:
        code, stderr = self.runner(self.args(path))
        if code != 0:
            raise PostError(f"ffmpeg (análisis) salió con {code}: {stderr[-500:]}")
        return parse_loudness(stderr)


def choose_analyzer(*, which: Callable[[str], str | None] | None = None) -> AudioAnalyzer:
    """``FfmpegLoudnessAnalyzer`` si ffmpeg está instalado; si no, ``NullAnalyzer`` con aviso."""
    ffmpeg = (which or shutil.which)("ffmpeg")
    if ffmpeg is None:
        logger.warning(
            "ffmpeg no está instalado: no se mide el loudness (ganancia en antena 0 dB)"
        )
        return NullAnalyzer()
    return FfmpegLoudnessAnalyzer(ffmpeg=ffmpeg)


# ── Análisis del stock existente (``radio analyze-loudness``) ──────────────────

@dataclass
class AnalyzeReport:
    """Resultado de ``analyze_stock``."""
    measured: int = 0
    skipped: int = 0          # ya tenían medida (``missing_only``) o sin audio en disco
    failed: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"Medidos: {self.measured}", f"Omitidos: {self.skipped}",
                 f"Fallidos: {len(self.failed)}"]
        lines += [f"  - {f}" for f in self.failed]
        return "\n".join(lines)


def analyze_stock(
    db: DB,
    analyzer: AudioAnalyzer,
    *,
    kind: str = "music",
    missing_only: bool = True,
) -> AnalyzeReport:
    """
    Mide (sin modificar) el audio de los segmentos ``ready`` de ``kind`` y guarda
    ``meta.loudness_lufs`` / ``meta.true_peak_db``. Sirve para el stock descargado
    antes de que existiera la medida. Con ``missing_only`` solo los que no la tienen.
    """
    report = AnalyzeReport()
    for seg in db.list_segments(kind=kind, status="ready"):
        if missing_only and isinstance(seg.meta.get("loudness_lufs"), int | float):
            report.skipped += 1
            continue
        if not seg.path.is_file():
            report.skipped += 1
            continue
        try:
            measured = analyzer.analyze(seg.path)
        except (PostError, OSError, subprocess.SubprocessError) as exc:
            report.failed.append(f"{seg.id} ({seg.title}): {exc}")
            continue
        if measured is None:
            report.failed.append(f"{seg.id} ({seg.title}): sin analizador (¿ffmpeg?)")
            continue
        meta = dict(seg.meta)
        meta["loudness_lufs"] = round(measured.integrated_lufs, 2)
        meta["true_peak_db"] = (
            None if measured.true_peak_db is None else round(measured.true_peak_db, 2)
        )
        db.update_segment_meta(seg.id, meta)
        report.measured += 1
    return report
