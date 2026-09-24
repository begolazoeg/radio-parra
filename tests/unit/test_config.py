"""
Tests de la configuración tipada (config/*.yaml, §5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from radio.core.config import (
    ProducerSettings,
    RadioConfig,
    StationConfig,
    VoiceEntry,
    VoicesConfig,
)
from radio.core.models import Voice

REPO = Path(__file__).parents[2]


def voice_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "locutor_principal", "role": "host", "provider": "cloud",
        "provider_voice_id": "<pendiente>", "consent": True,
        "consent_note": "Voz sintética genérica del proveedor",
    }
    data.update(overrides)
    return data


def test_repo_config_loads() -> None:
    cfg = RadioConfig.load(REPO / "config")
    st = cfg.station
    assert st.name == "Radio Parra" and st.language == "es"
    assert st.budget.monthly_eur == 10.0
    assert (st.providers["llm"].name, st.providers["llm"].model) == ("claude", "claude-sonnet-5")
    assert st.providers["tts"].name == "piper"
    assert cfg.data_dir == Path("data")

    assert [v.id for v in cfg.voices.voices] == ["locutor_principal"]
    host = cfg.voices.get("locutor_principal")
    assert isinstance(host, Voice)
    assert (host.role, host.provider, host.consent) == ("host", "piper", True)
    assert host.provider_voice_id == "es_ES-davefx-medium"
    assert cfg.voices.by_role("host") == [host]

    prods = cfg.producers.producers
    assert set(prods) == {"time_signal", "music_tinydesk"}
    ts = prods["time_signal"]
    assert (ts.active, ts.target_stock, ts.cron) == (True, 2, "*/30 * * * *")
    td = prods["music_tinydesk"]
    assert (td.active, td.target_stock, td.cron) == (True, 30, "0 */6 * * *")
    assert td.params["feed_url"] == "https://feeds.npr.org/510306/podcast.xml"  # decisión #8
    assert td.params["max_cache_items"] == 60
    assert cfg.producers.get("nope") is None


def test_station_rejects_legacy_budget_key() -> None:
    with pytest.raises(ValidationError):
        StationConfig.model_validate({"budget_monthly_eur": 5.0})
    assert StationConfig.model_validate({"budget": {"monthly_eur": 5}}).budget.monthly_eur == 5.0
    with pytest.raises(ValidationError):
        StationConfig.model_validate({"budget": {"monthly_eur": -1}})


@pytest.mark.parametrize(
    "overrides",
    [
        {"consent": False},
        {"consent_note": ""},
        {"consent_note": "   "},
        {"consent": "yes"},
    ],
)
def test_voice_requires_consent_and_note(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        VoiceEntry.model_validate(voice_data(**overrides))


def test_voice_requires_consent_note_key() -> None:
    data = voice_data()
    del data["consent_note"]
    with pytest.raises(ValidationError):
        VoiceEntry.model_validate(data)
    data = voice_data()
    del data["consent"]
    with pytest.raises(ValidationError):
        VoiceEntry.model_validate(data)


def test_voice_entry_to_voice() -> None:
    entry = VoiceEntry.model_validate(voice_data(
        id="dona_remedios", role="character", universe="consultorio", language="es",
        description="Doña Remedios",
    ))
    assert entry.to_voice() == Voice(
        id="dona_remedios", role="character", provider="cloud",
        provider_voice_id="<pendiente>", consent=True,
        consent_note="Voz sintética genérica del proveedor", language="es",
        universe="consultorio", description="Doña Remedios",
    )


def test_voices_unique_ids_and_unknown_lookup() -> None:
    with pytest.raises(ValidationError):
        VoicesConfig.model_validate({"voices": [voice_data(), voice_data()]})
    cfg = VoicesConfig.model_validate({"voices": [voice_data()]})
    with pytest.raises(ValueError):
        cfg.get("nope")


def test_invalid_voices_yaml_fails_load(tmp_path: Path) -> None:
    for name in ("station.yaml", "grid.yaml", "producers.yaml"):
        (tmp_path / name).write_text((REPO / "config" / name).read_text())
    (tmp_path / "voices.yaml").write_text(
        yaml.safe_dump({"voices": [voice_data(consent=False)]})
    )
    with pytest.raises(ValidationError):
        RadioConfig.load(tmp_path)


def test_producer_settings() -> None:
    s = ProducerSettings()
    assert (s.active, s.target_stock, s.cron, s.params) == (False, 0, None, {})
    with pytest.raises(ValidationError):
        ProducerSettings(cron="cada hora")
    with pytest.raises(ValidationError):
        ProducerSettings(target_stock=-1)
    with pytest.raises(ValidationError):
        ProducerSettings.model_validate({"interval_minutes": 30})
