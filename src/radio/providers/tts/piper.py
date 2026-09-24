"""
TTS local con Piper (§4.1: gratis, offline). Proveedor por defecto (decisión #3,
2026-09-24).

Se usa el binario ``piper`` como subproceso, sin dependencias de Python nuevas::

    echo "texto" | piper --model <models_dir>/<voz>.onnx --output_file <salida>.wav

- Binario y argumentos extra configurables (``providers.tts.extra.binary`` y
  ``extra.args``, p. ej. ``["--length_scale", "1.05"]``).
- El modelo de voz sale de ``voices.yaml → provider_voice_id`` resuelto contra
  ``extra.models_dir`` (``data/models/piper/`` por defecto): ``es_ES-davefx-medium``
  → ``data/models/piper/es_ES-davefx-medium.onnx`` (+ su ``.onnx.json``). También
  vale un nombre con ``.onnx`` o una ruta absoluta.
- La emisora **nunca** descarga modelos: se instalan a mano (ver voices.yaml, y
  revisar la licencia de cada modelo de voz antes de usarlo).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from radio.core.models import AudioInfo, Voice
from radio.providers.errors import ProviderNotAvailable, TTSError
from radio.providers.tts.base import require_consent, wav_duration

DEFAULT_MODELS_DIR = Path("data/models/piper")

# ``Voice.provider`` que este TTS acepta
PIPER_VOICE_PROVIDERS = frozenset({"piper", "local"})


def find_binary(binary: str) -> str | None:
    """Ruta del ejecutable (nombre en el PATH o ruta con directorio), o None."""
    return shutil.which(binary)


def resolve_model(provider_voice_id: str, models_dir: Path) -> Path:
    """Ruta del ``.onnx`` para ``provider_voice_id`` (ver docstring del módulo)."""
    name = provider_voice_id.strip()
    path = Path(name)
    if not path.is_absolute():
        path = models_dir / name
    if path.suffix != ".onnx":
        path = path.with_name(path.name + ".onnx")
    return path


class PiperTTS:
    """``TTS`` con el binario ``piper`` (ver docstring del módulo)."""

    name = "piper"

    def __init__(
        self,
        *,
        binary: str = "piper",
        models_dir: Path | str = DEFAULT_MODELS_DIR,
        args: Sequence[str] = (),
        timeout_s: float = 120.0,
        check_binary: bool = True,
    ) -> None:
        self.binary = binary
        self.models_dir = Path(models_dir)
        self.args = list(args)
        self.timeout_s = timeout_s
        if check_binary and find_binary(binary) is None:
            raise ProviderNotAvailable(
                f"no se encuentra el binario de Piper {binary!r} "
                "(instálalo o ajusta providers.tts.extra.binary)"
            )

    @property
    def cache_variant(self) -> str:
        """Parte de la clave de caché que depende de la configuración (args)."""
        return " ".join(self.args)

    def model_path(self, voice: Voice) -> Path:
        return resolve_model(voice.provider_voice_id, self.models_dir)

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        require_consent(voice)
        if voice.provider not in PIPER_VOICE_PROVIDERS:
            raise TTSError(
                f"la voz {voice.id!r} es del proveedor {voice.provider!r}, no de Piper"
            )
        if not text.strip():
            raise TTSError("texto vacío: nada que sintetizar")
        binary = find_binary(self.binary)
        if binary is None:
            raise ProviderNotAvailable(f"no se encuentra el binario de Piper {self.binary!r}")
        model = self.model_path(voice)
        if not model.is_file():
            raise ProviderNotAvailable(
                f"falta el modelo de voz de Piper {model} (voz {voice.id!r}); "
                "descárgalo a mano y revisa su licencia"
            )
        if not model.with_name(model.name + ".json").is_file():
            raise ProviderNotAvailable(f"falta la configuración del modelo {model}.json")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [binary, "--model", str(model), "--output_file", str(out_path), *self.args]
        try:
            proc = subprocess.run(
                cmd, input=text.encode("utf-8"), capture_output=True,
                timeout=self.timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out_path.unlink(missing_ok=True)
            raise TTSError(f"Piper tardó más de {self.timeout_s:.0f} s") from exc
        except OSError as exc:
            out_path.unlink(missing_ok=True)
            raise TTSError(f"no se pudo ejecutar Piper: {exc}") from exc
        if proc.returncode != 0:
            out_path.unlink(missing_ok=True)
            err = proc.stderr.decode("utf-8", "replace").strip()[-500:]
            raise TTSError(f"Piper terminó con código {proc.returncode}: {err}")
        if not out_path.is_file():
            raise TTSError(f"Piper no escribió {out_path}")
        try:
            duration = wav_duration(out_path)
        except Exception as exc:
            out_path.unlink(missing_ok=True)
            raise TTSError(f"Piper escribió un WAV ilegible: {exc}") from exc
        if duration <= 0:
            out_path.unlink(missing_ok=True)
            raise TTSError("Piper escribió un audio vacío")
        return AudioInfo(path=out_path, duration_s=duration)
