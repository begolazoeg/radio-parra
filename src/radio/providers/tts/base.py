"""
Protocolo base para proveedores TTS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from radio.core.models import AudioInfo, Voice


@runtime_checkable
class TTS(Protocol):
    """
    Interfaz mínima que todo proveedor TTS debe implementar (§4.1).
    Escribe el audio de ``text`` con ``voice`` en ``out_path`` (normalmente dentro
    de ``data/tmp/``; el llamador lo mueve después al stock).

    Opcional: ``preflight(voice)`` comprueba sin sintetizar (ni red ni coste) que la
    voz se puede usar y lanza ``ProviderNotAvailable``/``TTSError`` si no. Los
    productores de pago lo llaman antes del LLM (``radio.producers.base.preflight``)
    para no pagar un guion que luego no se podría locutar.
    """

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        ...


def wav_duration(path: Path) -> float:
    """Duración en segundos de un WAV PCM (módulo ``wave`` de la stdlib)."""
    import wave  # noqa: PLC0415

    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        return wf.getnframes() / rate if rate else 0.0


def require_consent(voice: Voice) -> None:
    """
    Invariante 9: ninguna voz sin ``consent: true``. voices.yaml ya lo valida al
    cargar; los TTS reales lo vuelven a comprobar por si alguien construye una
    ``Voice`` a mano.
    """
    from radio.providers.errors import TTSError  # noqa: PLC0415

    if voice.consent is not True or not voice.consent_note.strip():
        raise TTSError(f"voz {voice.id!r} sin consentimiento (voices.yaml): no se sintetiza")
