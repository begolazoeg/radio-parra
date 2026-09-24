"""
TTS en la nube: ElevenLabs por REST con ``httpx`` (§4.1). **Desactivado por
defecto** (decisión #3, 2026-09-24: el TTS principal es Piper local).

Petición (solo verificada contra *mocks*; **antes de activarlo hay que revisar el
endpoint, los parámetros y los formatos de salida en la documentación actual de
ElevenLabs**)::

    POST {base_url}/v1/text-to-speech/{voice_id}?output_format={output_format}
    xi-api-key: $ELEVENLABS_API_KEY
    {"text": ..., "model_id": ..., ["voice_settings": {...}], ["language_code": ...]}

Configuración (``providers.tts.extra``): ``base_url``, ``model_id``,
``output_format`` (``pcm_<hz>`` por defecto, que aquí se envuelve en WAV para que
el resto del pipeline trabaje siempre con WAV; también ``wav_<hz>`` o ``mp3_*``),
``voice_settings``, ``api_key_env`` y ``cost_eur_per_1k_chars`` (tarifa de tu plan,
para estimar el coste; 0 si no se conoce).

Coste: se factura por carácter; ``chars`` acumula los caracteres enviados y cada
productor ya los apunta en ``producer_runs.tts_chars``.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Any

import httpx

from radio.core.models import AudioInfo, Voice
from radio.providers.errors import ProviderNotAvailable, TTSError, TTSRetryableError
from radio.providers.tts.base import require_consent, wav_duration

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "pcm_22050"
DEFAULT_API_KEY_ENV = "ELEVENLABS_API_KEY"

# ``Voice.provider`` que este TTS acepta
CLOUD_VOICE_PROVIDERS = frozenset({"cloud", "elevenlabs"})


class CloudTTS:
    """``TTS`` con la API REST de ElevenLabs (ver docstring del módulo)."""

    name = "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        model_id: str = DEFAULT_MODEL_ID,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        voice_settings: dict[str, Any] | None = None,
        timeout_s: float = 60.0,
        cost_eur_per_1k_chars: float = 0.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        if not key.strip():
            raise ProviderNotAvailable(
                f"falta la clave del TTS en la nube ({api_key_env} en el entorno / .env)"
            )
        if not output_format.startswith(("pcm_", "wav_", "mp3_")):
            raise ValueError(f"output_format no soportado: {output_format!r}")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.output_format = output_format
        self.voice_settings = voice_settings
        self.cost_eur_per_1k_chars = cost_eur_per_1k_chars
        self.chars = 0          # caracteres enviados (facturables)
        self.requests = 0
        self._client = httpx.Client(
            headers={"xi-api-key": key, "Accept": "audio/*"},
            timeout=timeout_s,
            transport=transport,
        )

    @property
    def cache_variant(self) -> str:
        """Parte de la clave de caché que depende de la configuración."""
        return f"{self.model_id}|{self.output_format}"

    def estimate_cost_eur(self, chars: int) -> float:
        """Coste estimado de ``chars`` caracteres con la tarifa configurada."""
        return chars / 1000 * self.cost_eur_per_1k_chars

    def close(self) -> None:
        self._client.close()

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        require_consent(voice)
        if voice.provider not in CLOUD_VOICE_PROVIDERS:
            raise TTSError(
                f"la voz {voice.id!r} es del proveedor {voice.provider!r}, no de la nube"
            )
        voice_id = voice.provider_voice_id.strip()
        if not voice_id or voice_id.startswith("<"):
            raise TTSError(f"la voz {voice.id!r} no tiene provider_voice_id (voices.yaml)")
        if not text.strip():
            raise TTSError("texto vacío: nada que sintetizar")

        body: dict[str, Any] = {"text": text, "model_id": self.model_id}
        if self.voice_settings:
            body["voice_settings"] = self.voice_settings
        if voice.language:
            body["language_code"] = voice.language
        url = f"{self.base_url}/v1/text-to-speech/{voice_id}"
        try:
            resp = self._client.post(url, params={"output_format": self.output_format}, json=body)
        except httpx.HTTPError as exc:
            raise TTSRetryableError(f"sin conexión con el TTS en la nube: {exc}") from exc
        self.requests += 1
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TTSRetryableError(f"TTS en la nube: HTTP {resp.status_code}")
        if resp.status_code in (401, 403):
            raise TTSError(f"TTS en la nube: credenciales rechazadas (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise TTSError(f"TTS en la nube: HTTP {resp.status_code}: {resp.text[:300]}")
        audio = resp.content
        if not audio:
            raise TTSError("TTS en la nube: respuesta sin audio")
        self.chars += len(text)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            duration = self._write(audio, out_path)
        except Exception as exc:
            out_path.unlink(missing_ok=True)
            raise TTSError(f"TTS en la nube: audio ilegible ({self.output_format}): {exc}") from exc
        if duration <= 0:
            out_path.unlink(missing_ok=True)
            raise TTSError("TTS en la nube: audio vacío")
        return AudioInfo(path=out_path, duration_s=duration)

    def _write(self, audio: bytes, out_path: Path) -> float:
        """Escribe el audio en ``out_path`` y devuelve su duración."""
        fmt = self.output_format
        if fmt.startswith("pcm_"):
            # PCM crudo 16 bits mono little-endian → WAV
            rate = int(fmt.split("_")[1])
            with wave.open(str(out_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(audio)
            return wav_duration(out_path)
        out_path.write_bytes(audio)
        if fmt.startswith("wav_"):
            return wav_duration(out_path)
        import mutagen  # noqa: PLC0415

        audio_file = mutagen.File(out_path)
        info = getattr(audio_file, "info", None)
        return float(getattr(info, "length", 0.0) or 0.0)
