"""
Tests del registro de proveedores (station.yaml → LLM/TTS), sin red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radio.core.config import ProviderSettings, RadioConfig
from radio.core.models import Voice
from radio.core.store import DB
from radio.producers.runner import UnavailableLLM, UnavailableTTS, build_context
from radio.providers.llm.claude import ClaudeLLM
from radio.providers.llm.fake import FakeLLM
from radio.providers.registry import ProviderNotAvailable, build_llm, build_tts
from radio.providers.tts.cache import CachedTTS
from radio.providers.tts.cloud import CloudTTS
from radio.providers.tts.fake import FakeTTS
from radio.providers.tts.piper import PiperTTS
from tests.fixtures.providers import install_fake_piper

REPO = Path(__file__).parents[2]


@pytest.fixture
def no_claude_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                 "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "sin-perfil"))


def test_registry_builds_fakes() -> None:
    llm = build_llm(ProviderSettings(name="fake", extra={"fixture": {"script": "Hola."}}))
    assert isinstance(llm, FakeLLM) and llm.fixture == {"script": "Hola."}
    assert isinstance(build_llm(ProviderSettings(name="fake")), FakeLLM)
    tts = build_tts(ProviderSettings(name="fake", extra={"chars_per_second": 10}))
    assert isinstance(tts, FakeTTS) and tts.chars_per_second == 10.0


@pytest.mark.parametrize("settings", [None, ProviderSettings(name="gpt-algo")])
def test_registry_rejects_unknown(settings: ProviderSettings | None) -> None:
    with pytest.raises(ProviderNotAvailable):
        build_llm(settings)
    with pytest.raises(ProviderNotAvailable, match="tts"):
        build_tts(settings)


@pytest.mark.parametrize("name", ["claude", "anthropic", "Claude"])
def test_registry_builds_claude(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = build_llm(ProviderSettings(name=name, model="claude-sonnet-5", extra={
        "thinking": "disabled", "usd_to_eur": 0.9, "timeout_s": 30, "max_retries": 1,
        "prices": {"claude-sonnet-5": {"input": 1.0}},
    }))
    assert isinstance(llm, ClaudeLLM)
    assert (llm.model, llm.thinking, llm.usd_to_eur, llm.timeout_s, llm.max_retries) == (
        "claude-sonnet-5", "disabled", 0.9, 30.0, 1)
    assert llm.prices["claude-sonnet-5"] == {"input": 1.0, "output": 10.0, "cache_read": 0.2,
                                             "cache_write": 2.5}


@pytest.mark.usefixtures("no_claude_credentials")
def test_registry_claude_without_credentials() -> None:
    with pytest.raises(ProviderNotAvailable, match="ANTHROPIC_API_KEY"):
        build_llm(ProviderSettings(name="claude"))


def test_registry_builds_piper_with_cache(tmp_path: Path) -> None:
    exe = install_fake_piper(tmp_path)
    tts = build_tts(ProviderSettings(name="piper", extra={
        "binary": str(exe), "models_dir": str(tmp_path / "m"), "args": ["--x", 1],
        "cache_dir": str(tmp_path / "cache"),
    }))
    assert isinstance(tts, CachedTTS) and isinstance(tts.inner, PiperTTS)
    assert tts.name == "piper"
    assert tts.inner.models_dir == tmp_path / "m" and tts.inner.args == ["--x", "1"]
    assert isinstance(build_tts(ProviderSettings(name="local", extra={"binary": str(exe)})),
                      PiperTTS)


def test_registry_piper_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotAvailable, match="Piper"):
        build_tts(ProviderSettings(name="piper", extra={"binary": str(tmp_path / "nope")}))


@pytest.mark.parametrize("name", ["cloud", "elevenlabs"])
def test_registry_builds_cloud(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi")
    tts = build_tts(ProviderSettings(name=name, extra={"model_id": "m", "output_format": "mp3_44100_128"}))
    assert isinstance(tts, CloudTTS)
    assert (tts.model_id, tts.output_format) == ("m", "mp3_44100_128")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    with pytest.raises(ProviderNotAvailable, match="ELEVENLABS_API_KEY"):
        build_tts(ProviderSettings(name=name))


@pytest.mark.usefixtures("no_claude_credentials")
def test_repo_config_without_keys_or_piper_degrades(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin clave ni Piper, el contexto de producción usa sustitutos que fallan al usarse."""
    monkeypatch.setenv("PATH", str(tmp_path))            # sin piper en el PATH
    config = RadioConfig.load(REPO / "config")
    with DB(tmp_path / "state.db") as db:
        ctx = build_context(config, db, clock=None, data_dir=tmp_path)  # type: ignore[arg-type]
    assert isinstance(ctx.llm, UnavailableLLM) and isinstance(ctx.tts, UnavailableTTS)
    with pytest.raises(ProviderNotAvailable, match="ANTHROPIC_API_KEY"):
        ctx.llm.complete("s", "u", temperature=0.2)
    voice = Voice("v", "host", "piper", "x", True, "genérica")
    with pytest.raises(ProviderNotAvailable, match="Piper"):
        ctx.tts.synthesize("hola", voice, tmp_path / "o.wav")


def test_repo_config_builds_real_providers(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    exe = install_fake_piper(tmp_path)
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = RadioConfig.load(REPO / "config")
    llm = build_llm(config.station.providers["llm"])
    assert isinstance(llm, ClaudeLLM) and llm.model == "claude-sonnet-5"
    tts = build_tts(config.station.providers["tts"])
    assert isinstance(tts, CachedTTS) and isinstance(tts.inner, PiperTTS)
