"""
Tests de las comprobaciones de proveedores de ``radio doctor`` (sin red).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from radio.cli import app
from tests.fixtures.providers import install_fake_piper, install_piper_model

REPO = Path(__file__).parents[2]


def lines_by_label(output: str) -> dict[str, str]:
    return {line.split("] ", 1)[1].split(" — ")[0]: line
            for line in output.splitlines() if "] " in line}


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                 "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "sin-perfil"))
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("clean_env")
def test_doctor_warns_missing_credentials_piper_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(REPO / "config")])
    assert result.exit_code == 0, result.output
    lines = lines_by_label(result.output)
    assert "WARN" in lines["LLM claude (claude-sonnet-5) credenciales"]
    assert "ANTHROPIC_API_KEY" in lines["LLM claude (claude-sonnet-5) credenciales"]
    assert "WARN" in lines["TTS piper (piper)"]
    model = Path("data/models/piper/es_ES-davefx-medium.onnx")
    assert "WARN" in lines[f"voz locutor_principal ({model})"]
    assert not any(label.startswith("TTS cloud") for label in lines)   # solo si se configura


@pytest.mark.usefixtures("clean_env")
def test_doctor_ok_with_key_binary_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = install_fake_piper(tmp_path)
    install_piper_model(tmp_path / "data" / "models" / "piper", "es_ES-davefx-medium")
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(REPO / "config")])
    lines = lines_by_label(result.output)
    cred = lines["LLM claude (claude-sonnet-5) credenciales"]
    assert "OK" in cred and "ANTHROPIC_API_KEY presente" in cred
    assert "sk-test" not in result.output                             # nunca se imprime
    assert "OK" in lines["TTS piper (piper)"]
    model = Path("data/models/piper/es_ES-davefx-medium.onnx")
    assert "OK" in lines[f"voz locutor_principal ({model})"]


@pytest.mark.usefixtures("clean_env")
def test_doctor_checks_cloud_key_only_when_configured(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "station.yaml").write_text(
        "providers:\n  llm: {name: fake}\n  tts: {name: elevenlabs}\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["doctor", "--config-dir", str(config)])
    lines = lines_by_label(result.output)
    assert "WARN" in lines["TTS cloud (ELEVENLABS_API_KEY)"]
    assert "OK" in lines["LLM fake"]
