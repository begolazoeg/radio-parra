"""
Tests del registro de proveedores (solo fakes en Fase 1).
"""

from __future__ import annotations

import pytest

from radio.core.config import ProviderSettings
from radio.providers.llm.fake import FakeLLM
from radio.providers.registry import ProviderNotAvailable, build_llm, build_tts
from radio.providers.tts.fake import FakeTTS


def test_registry_builds_fakes() -> None:
    llm = build_llm(ProviderSettings(name="fake", extra={"fixture": {"script": "Hola."}}))
    assert isinstance(llm, FakeLLM) and llm.fixture == {"script": "Hola."}
    assert isinstance(build_llm(ProviderSettings(name="fake")), FakeLLM)
    tts = build_tts(ProviderSettings(name="fake", extra={"chars_per_second": 10}))
    assert isinstance(tts, FakeTTS) and tts.chars_per_second == 10.0


@pytest.mark.parametrize("settings", [None, ProviderSettings(name="anthropic")])
def test_registry_rejects_unknown(settings: ProviderSettings | None) -> None:
    with pytest.raises(ProviderNotAvailable):
        build_llm(settings)
    with pytest.raises(ProviderNotAvailable, match="tts"):
        build_tts(settings)
