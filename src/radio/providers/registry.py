"""
Registro de proveedores: construye el LLM y el TTS a partir de station.yaml
(``providers.llm`` / ``providers.tts``, invariante 6).

LLM (``providers.llm.name``):

- ``fake``: ``FakeLLM`` (``extra.fixture`` fija la respuesta).
- ``claude`` (alias ``anthropic``): ``ClaudeLLM`` con ``model`` (por defecto
  ``claude-sonnet-5``) y ``extra``: ``thinking`` (``adaptive``|``disabled``),
  ``effort``, ``min_max_tokens_adaptive``, ``timeout_s``, ``max_retries``,
  ``usd_to_eur``, ``prices``.

TTS (``providers.tts.name``):

- ``fake``: ``FakeTTS`` (``extra.chars_per_second``).
- ``piper`` (alias ``local``): ``PiperTTS`` con ``extra.binary``,
  ``extra.models_dir``, ``extra.args``, ``extra.timeout_s``.
- ``cloud`` (alias ``elevenlabs``): ``CloudTTS`` con ``extra.base_url``,
  ``model_id``, ``output_format``, ``voice_settings``, ``api_key_env``,
  ``timeout_s``, ``cost_eur_per_1k_chars``.
- Con ``extra.cache_dir`` el TTS se envuelve en ``CachedTTS``.

Si falta algo local (binario, clave, credenciales) se lanza
``ProviderNotAvailable``: ``producers.runner.build_context`` lo captura y pone un
sustituto que falla al usarse, así que los productores que lo necesitan registran
el error y los demás (música) siguen. La emisora nunca importa este módulo
(invariante 2).
"""

from __future__ import annotations

from typing import Any

from radio.core.config import ProviderSettings
from radio.providers.errors import ProviderNotAvailable
from radio.providers.llm.base import LLM
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.base import TTS
from radio.providers.tts.fake import FakeTTS

__all__ = ["LLM_PROVIDERS", "TTS_PROVIDERS", "ProviderNotAvailable", "build_llm", "build_tts"]

# Nombre en station.yaml → nombre canónico
LLM_PROVIDERS: dict[str, str] = {"fake": "fake", "claude": "claude", "anthropic": "claude"}
TTS_PROVIDERS: dict[str, str] = {
    "fake": "fake",
    "piper": "piper",
    "local": "piper",
    "cloud": "cloud",
    "elevenlabs": "cloud",
}


def _require(settings: ProviderSettings | None, what: str, known: dict[str, str]) -> str:
    """Nombre canónico del proveedor configurado, o ``ProviderNotAvailable``."""
    if settings is None:
        raise ProviderNotAvailable(
            f"No hay proveedor {what} configurado (station.yaml → providers.{what})"
        )
    canonical = known.get(settings.name.strip().lower())
    if canonical is None:
        raise ProviderNotAvailable(
            f"Proveedor {what} {settings.name!r} no disponible "
            f"(soportados: {', '.join(sorted(known))})"
        )
    return canonical


def _float(extra: dict[str, Any], key: str, default: float) -> float:
    value = extra.get(key)
    return default if value is None else float(value)


def build_llm(settings: ProviderSettings | None) -> LLM:
    """Construye el LLM configurado (ver docstring del módulo)."""
    name = _require(settings, "llm", LLM_PROVIDERS)
    assert settings is not None
    extra = settings.extra
    if name == "fake":
        return FakeLLM(extra.get("fixture"))

    from radio.providers.llm.claude import (  # noqa: PLC0415
        DEFAULT_MIN_MAX_TOKENS_ADAPTIVE,
        DEFAULT_MODEL,
        DEFAULT_USD_TO_EUR,
        ClaudeLLM,
        detect_credentials,
    )

    if extra.get("require_credentials", True) and detect_credentials() is None:
        raise ProviderNotAvailable(
            "no hay credenciales de Claude (ANTHROPIC_API_KEY en el entorno / .env, "
            "o un perfil de `ant auth login`)"
        )
    return ClaudeLLM(
        settings.model or DEFAULT_MODEL,
        timeout_s=_float(extra, "timeout_s", 60.0),
        max_retries=int(extra.get("max_retries", 2)),
        thinking=extra.get("thinking", "adaptive"),
        effort=extra.get("effort", "low"),
        min_max_tokens_adaptive=int(
            extra.get("min_max_tokens_adaptive", DEFAULT_MIN_MAX_TOKENS_ADAPTIVE)
        ),
        usd_to_eur=_float(extra, "usd_to_eur", DEFAULT_USD_TO_EUR),
        prices=extra.get("prices"),
    )


def build_tts(settings: ProviderSettings | None) -> TTS:
    """Construye el TTS configurado, con caché si hay ``extra.cache_dir``."""
    name = _require(settings, "tts", TTS_PROVIDERS)
    assert settings is not None
    extra = settings.extra
    tts: TTS
    if name == "fake":
        cps = extra.get("chars_per_second")
        tts = FakeTTS() if cps is None else FakeTTS(chars_per_second=float(cps))
    elif name == "piper":
        from radio.providers.tts.piper import DEFAULT_MODELS_DIR, PiperTTS  # noqa: PLC0415

        tts = PiperTTS(
            binary=str(extra.get("binary", "piper")),
            models_dir=extra.get("models_dir", DEFAULT_MODELS_DIR),
            args=[str(a) for a in extra.get("args", [])],
            timeout_s=_float(extra, "timeout_s", 120.0),
        )
    else:
        from radio.providers.tts import cloud  # noqa: PLC0415

        tts = cloud.CloudTTS(
            api_key_env=str(extra.get("api_key_env", cloud.DEFAULT_API_KEY_ENV)),
            base_url=str(extra.get("base_url", cloud.DEFAULT_BASE_URL)),
            model_id=str(settings.model or extra.get("model_id", cloud.DEFAULT_MODEL_ID)),
            output_format=str(extra.get("output_format", cloud.DEFAULT_OUTPUT_FORMAT)),
            voice_settings=extra.get("voice_settings"),
            timeout_s=_float(extra, "timeout_s", 60.0),
            cost_eur_per_1k_chars=_float(extra, "cost_eur_per_1k_chars", 0.0),
        )

    cache_dir = extra.get("cache_dir")
    if cache_dir:
        from radio.providers.tts.cache import CachedTTS  # noqa: PLC0415

        return CachedTTS(tts, cache_dir)
    return tts
