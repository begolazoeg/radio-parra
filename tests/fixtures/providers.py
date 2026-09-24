"""
Ayudas para los tests de proveedores (sin red): binario ``piper`` falso, modelos de
voz vacíos, voces de prueba y un transporte HTTP simulado para ElevenLabs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

from radio.core.models import Voice

FAKE_PIPER = Path(__file__).with_name("fake_piper.py")


def install_fake_piper(tmp_path: Path) -> Path:
    """Ejecutable ``piper`` falso (script con el Python actual en el shebang)."""
    exe = tmp_path / "bin" / "piper"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!{sys.executable}\n" + FAKE_PIPER.read_text(encoding="utf-8"),
                   encoding="utf-8")
    exe.chmod(0o755)
    return exe


def install_piper_model(models_dir: Path, name: str = "es_ES-test-medium") -> Path:
    """Modelo de voz vacío (``.onnx`` + ``.onnx.json``): solo para el doble de Piper."""
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / f"{name}.onnx"
    onnx.write_bytes(b"")
    (models_dir / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    return onnx


def make_voice(provider: str = "piper", provider_voice_id: str = "es_ES-test-medium",
               **kw: Any) -> Voice:
    data: dict[str, Any] = {
        "id": "locutor_test", "role": "host", "provider": provider,
        "provider_voice_id": provider_voice_id, "consent": True,
        "consent_note": "Voz sintética genérica de prueba",
    }
    data.update(kw)
    return Voice(**data)


class ElevenLabsMock:
    """Transporte ``httpx.MockTransport`` que imita ``POST /v1/text-to-speech/{id}``."""

    def __init__(self, status: int = 200, rate: int = 22050) -> None:
        self.status = status
        self.rate = rate
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "error simulado"})
        text = json.loads(request.content)["text"]
        seconds = max(0.1, len(text) / 15)
        pcm = b"\x00\x00" * int(self.rate * seconds)
        return httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
