"""
Configuración tipada de Radio Parra usando Pydantic v2.
Carga los 4 archivos YAML desde config/ sin requerir .env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Modelos de sub-configuración ──────────────────────────────────────────────

class ProviderSettings(BaseModel):
    """Configuración de un proveedor externo (LLM, TTS, etc.)."""
    name: str
    model: Optional[str] = None
    extra: dict[str, Any] = {}


class StationConfig(BaseModel):
    """Configuración global de la emisora (station.yaml)."""
    timezone: str = "Europe/Madrid"
    language: str = "es"
    data_dir: str = "data"
    budget_monthly_eur: float = 10.0
    loudness_lufs: float = -16.0
    providers: dict[str, ProviderSettings] = {}


class TimeSlot(BaseModel):
    """Franja horaria con configuración de programación."""
    name: str
    start: str          # "HH:MM"
    end: str            # "HH:MM"
    music_ratio: float = 0.7
    max_talk_run_min: int = 3


class Cooldowns(BaseModel):
    """Tiempos mínimos entre segmentos del mismo tipo (minutos)."""
    factual: int = 15
    fiction: int = 30
    time_signal: int = 55
    jingle: int = 10


class GridConfig(BaseModel):
    """Configuración del scheduler de parrilla (grid.yaml)."""
    timezone: str = "Europe/Madrid"
    talk_budget_ratio: float = 0.3
    cooldowns_minutes: Cooldowns = Cooldowns()
    slots: list[TimeSlot] = []
    time_signal_enabled: bool = True


class VoiceEntry(BaseModel):
    """Entrada individual de voices.yaml."""
    id: str
    name: str
    provider: str
    consent: bool
    language: str = "es"
    description: str = ""

    @field_validator("consent")
    @classmethod
    def consent_must_be_true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Voice consent must be True — using a voice without consent is not allowed")
        return v


class VoicesConfig(BaseModel):
    """Lista de voces disponibles (voices.yaml)."""
    voices: list[VoiceEntry] = []


class ProducerSettings(BaseModel):
    """Configuración de un producer individual."""
    active: bool = False
    interval_minutes: int = 60
    extra: dict[str, Any] = {}


class ProducersConfig(BaseModel):
    """Mapa de producers y su configuración (producers.yaml)."""
    producers: dict[str, ProducerSettings] = {}


# ── RadioConfig: carga los 4 archivos ─────────────────────────────────────────

def _load_yaml(path: Path) -> dict[str, Any]:
    """Carga un archivo YAML; devuelve dict vacío si no existe."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class RadioConfig(BaseModel):
    """
    Configuración raíz de la radio.
    Carga los 4 archivos YAML desde config_dir (por defecto: config/).
    """
    config_dir: Path = Path("config")
    station: StationConfig = StationConfig()
    grid: GridConfig = GridConfig()
    voices: VoicesConfig = VoicesConfig()
    producers: ProducersConfig = ProducersConfig()

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def load(cls, config_dir: Path = Path("config")) -> "RadioConfig":
        """Carga todos los archivos de configuración desde config_dir."""
        station_data = _load_yaml(config_dir / "station.yaml")
        grid_data = _load_yaml(config_dir / "grid.yaml")
        voices_data = _load_yaml(config_dir / "voices.yaml")
        producers_data = _load_yaml(config_dir / "producers.yaml")

        return cls(
            config_dir=config_dir,
            station=StationConfig(**station_data),
            grid=GridConfig(**grid_data),
            voices=VoicesConfig(**voices_data),
            producers=ProducersConfig(**producers_data),
        )
