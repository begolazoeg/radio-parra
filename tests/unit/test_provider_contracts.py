"""
Tests de contrato de los proveedores (§9): la misma suite corre contra el fake y
contra cada implementación real con su dependencia externa simulada (SDK de
Claude con un cliente doble, Piper con un binario falso, ElevenLabs con
``httpx.MockTransport``).

Las variantes marcadas ``real_provider`` usan los servicios de verdad (red,
claves, Piper instalado): solo corren a mano con ``RADIO_REAL_PROVIDERS=1`` y
nunca en CI. Variables opcionales: ``RADIO_PIPER_BINARY``, ``RADIO_PIPER_MODELS``,
``RADIO_PIPER_VOICE``, ``ELEVENLABS_VOICE_ID``.
"""

from __future__ import annotations

import json
import os
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from radio.core.models import AudioInfo, LLMResult, Voice
from radio.providers.llm.base import LLM
from radio.providers.llm.claude import ClaudeLLM
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.base import TTS
from radio.providers.tts.cache import CachedTTS
from radio.providers.tts.cloud import CloudTTS
from radio.providers.tts.fake import FakeTTS
from radio.providers.tts.piper import PiperTTS
from tests.fixtures.providers import (
    ElevenLabsMock,
    install_fake_piper,
    install_piper_model,
    make_voice,
)
from tests.unit.test_llm_claude import FakeClient, make_message

REAL = os.environ.get("RADIO_REAL_PROVIDERS") == "1"
real_provider = [
    pytest.mark.real_provider,
    pytest.mark.skipif(not REAL, reason="proveedor real: solo con RADIO_REAL_PROVIDERS=1"),
]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"script": {"type": "string"}},
    "required": ["script"],
    "additionalProperties": False,
}
FIXTURE = {"script": "Buenas tardes, esto es Radio Parra."}


# ── LLM ───────────────────────────────────────────────────────────────────────

@pytest.fixture(params=[
    "fake",
    "claude-mock",
    pytest.param("claude-real", marks=real_provider),
])
def llm(request: pytest.FixtureRequest) -> LLM:
    if request.param == "fake":
        return FakeLLM(FIXTURE)
    if request.param == "claude-mock":
        return ClaudeLLM(client=FakeClient(make_message(json.dumps(FIXTURE))))
    return ClaudeLLM()        # credenciales del entorno; red de verdad


def test_llm_contract_protocol(llm: LLM) -> None:
    assert isinstance(llm, LLM)


def test_llm_contract_json_schema(llm: LLM) -> None:
    res = llm.complete(
        "Eres la locutora de una radio casera. Responde en español.",
        "Escribe un saludo de una frase para la radio.",
        temperature=0.2, json_schema=SCHEMA, max_tokens=300,
    )
    assert isinstance(res, LLMResult)
    data = json.loads(res.text)
    assert isinstance(data, dict) and isinstance(data["script"], str) and data["script"]
    assert res.input_tokens >= 0 and res.output_tokens >= 0
    assert res.cost_eur >= 0 and isinstance(res.model, str)


@pytest.mark.parametrize("temperature", [0.0, 0.3, 1.0])
def test_llm_contract_accepts_any_temperature(llm: LLM, temperature: float) -> None:
    """Inv. 6: la firma acepta temperature aunque el modelo la ignore."""
    res = llm.complete("Responde en español.", "Di hola.", temperature=temperature)
    assert isinstance(res.text, str) and res.text


# ── TTS ───────────────────────────────────────────────────────────────────────

def _piper_real(tmp_path: Path) -> tuple[TTS, Voice]:
    tts = PiperTTS(binary=os.environ.get("RADIO_PIPER_BINARY", "piper"),
                   models_dir=os.environ.get("RADIO_PIPER_MODELS", "data/models/piper"))
    voice = make_voice(provider_voice_id=os.environ.get("RADIO_PIPER_VOICE",
                                                        "es_ES-davefx-medium"))
    return tts, voice


def _cloud_real(tmp_path: Path) -> tuple[TTS, Voice]:
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "")
    if not voice_id:
        pytest.skip("falta ELEVENLABS_VOICE_ID")
    return CloudTTS(), make_voice(provider="cloud", provider_voice_id=voice_id)


@pytest.fixture(params=[
    "fake",
    "cached-fake",
    "piper-fake-binary",
    "cloud-mock",
    pytest.param("piper-real", marks=real_provider),
    pytest.param("cloud-real", marks=real_provider),
])
def tts_and_voice(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[TTS, Voice]]:
    kind = request.param
    if kind == "fake":
        yield FakeTTS(), make_voice(provider="fake")
    elif kind == "cached-fake":
        yield CachedTTS(FakeTTS(), tmp_path / "cache"), make_voice(provider="fake")
    elif kind == "piper-fake-binary":
        exe = install_fake_piper(tmp_path)
        install_piper_model(tmp_path / "models")
        yield PiperTTS(binary=str(exe), models_dir=tmp_path / "models"), make_voice()
    elif kind == "cloud-mock":
        tts = CloudTTS(api_key="xi-test", transport=ElevenLabsMock().transport)
        yield tts, make_voice(provider="cloud", provider_voice_id="voz-test")
        tts.close()
    elif kind == "piper-real":
        yield _piper_real(tmp_path)
    else:
        yield _cloud_real(tmp_path)


def test_tts_contract_protocol(tts_and_voice: tuple[TTS, Voice]) -> None:
    assert isinstance(tts_and_voice[0], TTS)


def test_tts_contract_writes_wav(tts_and_voice: tuple[TTS, Voice], tmp_path: Path) -> None:
    tts, voice = tts_and_voice
    out = tmp_path / "tmp" / "nuevo" / "saludo.wav"        # el directorio no existe
    info = tts.synthesize("Buenas tardes, esto es Radio Parra.", voice, out)
    assert isinstance(info, AudioInfo)
    assert info.path == out and out.is_file()
    assert info.duration_s > 0
    with wave.open(str(out), "rb") as wf:                  # WAV legible
        assert wf.getnframes() / wf.getframerate() == pytest.approx(info.duration_s, rel=0.05)


def test_tts_contract_longer_text_lasts_longer(
    tts_and_voice: tuple[TTS, Voice], tmp_path: Path
) -> None:
    tts, voice = tts_and_voice
    short = tts.synthesize("Hola.", voice, tmp_path / "a.wav")
    long = tts.synthesize(
        "Hola. Son las ocho y veinte y en Radio Parra seguimos con música de Tiny Desk.",
        voice, tmp_path / "b.wav",
    )
    assert long.duration_s > short.duration_s


def test_tts_contract_repeatable(tts_and_voice: tuple[TTS, Voice], tmp_path: Path) -> None:
    """Sintetizar lo mismo dos veces da dos archivos válidos (con o sin caché)."""
    tts, voice = tts_and_voice
    a = tts.synthesize("Radio Parra.", voice, tmp_path / "a.wav")
    b = tts.synthesize("Radio Parra.", voice, tmp_path / "b.wav")
    assert a.path.is_file() and b.path.is_file() and a.path != b.path
    assert a.duration_s == pytest.approx(b.duration_s, rel=0.1)
