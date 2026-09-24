"""
Registro de proveedores: construye el LLM y el TTS a partir de station.yaml.

Fase 1: solo existen los proveedores "fake" (sin red). Cualquier otro nombre
lanza ``ProviderNotAvailable`` con un mensaje claro; la emisora lo captura y
arranca en modo solo música.
"""

from __future__ import annotations

from radio.core.config import ProviderSettings
from radio.providers.llm.base import LLM
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.base import TTS
from radio.providers.tts.fake import FakeTTS

# Guion por defecto del LLM fake (respuesta JSON de la locutora)
DEFAULT_FAKE_SCRIPT = (
    "Buenas, seguís escuchando Radio Parra, la radio de los conciertos pequeños. "
    "Soy la locutora artificial de la casa y me encanta acompañaros mientras sonáis "
    "de fondo en la cocina. Sin más prisa, volvemos con más música en directo."
)

SUPPORTED = ("fake",)


class ProviderNotAvailable(RuntimeError):
    """El proveedor configurado no existe o todavía no está implementado."""


def _require(settings: ProviderSettings | None, what: str) -> ProviderSettings:
    if settings is None:
        raise ProviderNotAvailable(
            f"No hay proveedor {what} configurado (station.yaml → providers.{what})"
        )
    if settings.name not in SUPPORTED:
        raise ProviderNotAvailable(
            f"Proveedor {what} {settings.name!r} no disponible todavía "
            f"(soportados: {', '.join(SUPPORTED)})"
        )
    return settings


def build_llm(settings: ProviderSettings | None) -> LLM:
    """Construye el LLM configurado. ``extra.fixture`` fija la respuesta del fake."""
    cfg = _require(settings, "llm")
    return FakeLLM(cfg.extra.get("fixture", {"script": DEFAULT_FAKE_SCRIPT}))


def build_tts(settings: ProviderSettings | None) -> TTS:
    """Construye el TTS configurado. ``extra.chars_per_second`` ajusta el fake."""
    cfg = _require(settings, "tts")
    cps = cfg.extra.get("chars_per_second")
    return FakeTTS() if cps is None else FakeTTS(chars_per_second=float(cps))
