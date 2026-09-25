"""
Tests específicos de los TTS reales (Piper, nube) y de la caché, sin red ni Piper
instalado: Piper se sustituye por un script falso y ElevenLabs por un transporte
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radio.providers.errors import (
    ProviderNotAvailable,
    TTSError,
    TTSRetryableError,
)
from radio.providers.tts.base import wav_duration
from radio.providers.tts.cache import CachedTTS, cache_key
from radio.providers.tts.cloud import CloudTTS
from radio.providers.tts.fake import FakeTTS
from radio.providers.tts.piper import PiperTTS, resolve_model
from tests.fixtures.providers import (
    ElevenLabsMock,
    install_fake_piper,
    install_piper_model,
    make_voice,
)

# ── Piper ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def piper(tmp_path: Path) -> PiperTTS:
    exe = install_fake_piper(tmp_path)
    install_piper_model(tmp_path / "models")
    return PiperTTS(binary=str(exe), models_dir=tmp_path / "models",
                    args=["--length_scale", "1.05"])


def test_piper_runs_binary_with_model_and_text(piper: PiperTTS, tmp_path: Path) -> None:
    out = tmp_path / "tmp" / "intro.wav"
    info = piper.synthesize("Buenas noches desde la parra.", make_voice(), out)
    assert info.path == out and out.is_file()
    assert info.duration_s == pytest.approx(wav_duration(out)) and info.duration_s > 0
    call = json.loads(Path(str(out) + ".args.json").read_text())
    assert call["text"] == "Buenas noches desde la parra."
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == str(tmp_path / "models" / "es_ES-test-medium.onnx")
    assert argv[argv.index("--output_file") + 1] == str(out)
    assert argv[-2:] == ["--length_scale", "1.05"]


def test_piper_missing_binary_is_not_available(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotAvailable, match="binario"):
        PiperTTS(binary=str(tmp_path / "no-existe"))


def test_piper_missing_model(piper: PiperTTS, tmp_path: Path) -> None:
    with pytest.raises(ProviderNotAvailable, match="modelo"):
        piper.synthesize("Hola", make_voice(provider_voice_id="es_ES-otra-low"),
                         tmp_path / "o.wav")


def test_piper_failure_cleans_output(piper: PiperTTS, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_PIPER_FAIL", "1")
    out = tmp_path / "o.wav"
    with pytest.raises(TTSError, match="fallo simulado"):
        piper.synthesize("Hola", make_voice(), out)
    assert not out.exists()


def test_piper_rejects_other_provider_voice_and_no_consent(piper: PiperTTS,
                                                           tmp_path: Path) -> None:
    with pytest.raises(TTSError, match="proveedor"):
        piper.synthesize("Hola", make_voice(provider="cloud"), tmp_path / "o.wav")
    with pytest.raises(TTSError, match="consentimiento"):
        piper.synthesize("Hola", make_voice(consent=False), tmp_path / "o.wav")
    with pytest.raises(TTSError, match="vacío"):
        piper.synthesize("   ", make_voice(), tmp_path / "o.wav")


def test_resolve_model(tmp_path: Path) -> None:
    assert resolve_model("es_ES-x-medium", tmp_path) == tmp_path / "es_ES-x-medium.onnx"
    assert resolve_model("es_ES-x-medium.onnx", tmp_path) == tmp_path / "es_ES-x-medium.onnx"
    assert resolve_model("/abs/v.onnx", tmp_path) == Path("/abs/v.onnx")


# ── Nube (ElevenLabs) ─────────────────────────────────────────────────────────

def cloud(mock: ElevenLabsMock, **kw: object) -> CloudTTS:
    return CloudTTS(api_key="xi-test", transport=mock.transport, **kw)  # type: ignore[arg-type]


def test_cloud_request_shape(tmp_path: Path) -> None:
    mock = ElevenLabsMock()
    tts = cloud(mock, base_url="https://tts.example/", model_id="modelo-x",
                voice_settings={"stability": 0.5}, cost_eur_per_1k_chars=0.3)
    text = "Son las ocho en Radio Parra."
    info = tts.synthesize(text, make_voice(provider="cloud", provider_voice_id="voz123"),
                          tmp_path / "o.wav")
    (req,) = mock.requests
    assert req.method == "POST"
    assert req.url.path == "/v1/text-to-speech/voz123"
    assert req.url.host == "tts.example"
    assert req.url.params["output_format"] == "pcm_22050"
    assert req.headers["xi-api-key"] == "xi-test"
    body = json.loads(req.content)
    assert body == {"text": text, "model_id": "modelo-x", "voice_settings": {"stability": 0.5},
                    "language_code": "es"}
    # PCM envuelto en WAV: duración legible con wave
    assert info.duration_s == pytest.approx(len(text) / 15, rel=0.01)
    assert tts.chars == len(text) and tts.requests == 1
    assert tts.estimate_cost_eur(tts.chars) == pytest.approx(len(text) / 1000 * 0.3)


def test_cloud_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ProviderNotAvailable, match="ELEVENLABS_API_KEY"):
        CloudTTS()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi")
    assert CloudTTS(transport=ElevenLabsMock().transport).chars == 0


@pytest.mark.parametrize(("status", "exc"), [
    (429, TTSRetryableError), (503, TTSRetryableError), (401, TTSError), (400, TTSError),
])
def test_cloud_http_errors(tmp_path: Path, status: int, exc: type[TTSError]) -> None:
    tts = cloud(ElevenLabsMock(status=status))
    with pytest.raises(exc) as info:
        tts.synthesize("Hola", make_voice(provider="cloud", provider_voice_id="v"),
                       tmp_path / "o.wav")
    assert info.value.retryable is (exc is TTSRetryableError)
    assert tts.chars == 0


def test_cloud_rejects_placeholder_voice(tmp_path: Path) -> None:
    with pytest.raises(TTSError, match="provider_voice_id"):
        cloud(ElevenLabsMock()).synthesize(
            "Hola", make_voice(provider="cloud", provider_voice_id="<pendiente>"),
            tmp_path / "o.wav")


# ── Caché ─────────────────────────────────────────────────────────────────────

def test_cache_hit_and_miss(tmp_path: Path) -> None:
    inner = FakeTTS()
    tts = CachedTTS(inner, tmp_path / "cache")
    voice = make_voice(provider="fake")
    a = tts.synthesize("Hola, parra.", voice, tmp_path / "a.wav")
    b = tts.synthesize("Hola, parra.", voice, tmp_path / "b.wav")
    assert (tts.misses, tts.hits, len(inner.calls)) == (1, 1, 1)
    assert b.path == tmp_path / "b.wav" and b.path.read_bytes() == a.path.read_bytes()
    assert b.duration_s == a.duration_s
    tts.synthesize("Otro texto.", voice, tmp_path / "c.wav")
    other_voice = make_voice(provider="fake", id="otra")
    tts.synthesize("Hola, parra.", other_voice, tmp_path / "d.wav")
    assert (tts.misses, tts.hits, len(inner.calls)) == (3, 1, 3)


def test_cache_key_components() -> None:
    v = make_voice()
    base = cache_key("t", v, "piper")
    assert base != cache_key("t2", v, "piper")
    assert base != cache_key("t", v, "elevenlabs")
    assert base != cache_key("t", make_voice(provider_voice_id="otra"), "piper")
    assert base != cache_key("t", make_voice(id="otra"), "piper")
    assert base == cache_key("t", make_voice(), "piper")


def test_cache_ignores_corrupt_entry(tmp_path: Path) -> None:
    tts = CachedTTS(FakeTTS(), tmp_path / "cache")
    voice = make_voice(provider="fake")
    tts.synthesize("Hola", voice, tmp_path / "a.wav")
    key = tts.key("Hola", voice)
    (tmp_path / "cache" / key[:2] / f"{key}.json").write_text("roto")
    tts.synthesize("Hola", voice, tmp_path / "b.wav")
    assert (tts.misses, tts.hits) == (2, 0)


def test_cache_does_not_store_failures(tmp_path: Path) -> None:
    class Broken:
        name = "broken"

        def synthesize(self, *a: object) -> None:
            raise TTSError("no")

    tts = CachedTTS(Broken(), tmp_path / "cache")  # type: ignore[arg-type]
    with pytest.raises(TTSError):
        tts.synthesize("Hola", make_voice(), tmp_path / "a.wav")
    assert not any((tmp_path / "cache").rglob("*.audio"))
