"""
Configuración tipada de Radio Parra usando Pydantic v2.
Carga los 4 archivos YAML desde config/ sin requerir .env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from radio.core.cron import parse_cron
from radio.core.models import Voice

# ── Modelos de sub-configuración ──────────────────────────────────────────────

class ProviderSettings(BaseModel):
    """Configuración de un proveedor externo (LLM, TTS, etc.)."""
    name: str
    model: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    """Presupuesto de gasto en APIs externas (§4.2, regla de gasto)."""
    model_config = ConfigDict(extra="forbid")

    monthly_eur: float = Field(default=10.0, ge=0.0)


class StationConfig(BaseModel):
    """Configuración global de la emisora (station.yaml)."""
    model_config = ConfigDict(extra="forbid")

    name: str = "Radio Parra"           # provisional: decisión abierta #1
    timezone: str = "Europe/Madrid"
    language: str = "es"                # decisión abierta #2
    data_dir: str = "data"
    budget: BudgetConfig = BudgetConfig()
    loudness_lufs: float = -16.0
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


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
    host_intro: int = 12


class GridConfig(BaseModel):
    """Configuración del scheduler de parrilla (grid.yaml)."""
    timezone: str = "Europe/Madrid"
    talk_budget_ratio: float = 0.3
    cooldowns_minutes: Cooldowns = Cooldowns()
    slots: list[TimeSlot] = Field(default_factory=list)
    time_signal_enabled: bool = True


class VoiceEntry(BaseModel):
    """
    Entrada de voices.yaml (§5). Regla de carga: toda voz necesita
    ``consent: true`` y una ``consent_note`` no vacía (invariante §1.9).
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    role: str = Field(min_length=1)               # "host" | "character" | ...
    provider: str = Field(min_length=1)           # "cloud" | "local" | ...
    provider_voice_id: str = Field(min_length=1)
    language: str = "es"
    consent: StrictBool                           # literalmente true en el YAML
    consent_note: str
    universe: str | None = None
    description: str = ""

    @field_validator("consent")
    @classmethod
    def consent_must_be_true(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("consent debe ser true: no se usa ninguna voz sin consentimiento")
        return v

    @field_validator("consent_note")
    @classmethod
    def consent_note_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("consent_note es obligatoria y no puede estar vacía")
        return v

    def to_voice(self) -> Voice:
        """Convierte la entrada validada en el modelo de dominio ``Voice``."""
        return Voice(
            id=self.id,
            role=self.role,
            provider=self.provider,
            provider_voice_id=self.provider_voice_id,
            consent=self.consent,
            consent_note=self.consent_note,
            language=self.language,
            universe=self.universe,
            description=self.description,
        )


class VoicesConfig(BaseModel):
    """Lista de voces disponibles (voices.yaml)."""
    voices: list[VoiceEntry] = Field(default_factory=list)

    @field_validator("voices")
    @classmethod
    def unique_ids(cls, v: list[VoiceEntry]) -> list[VoiceEntry]:
        ids = [voice.id for voice in v]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"ids de voz repetidos: {dupes}")
        return v

    def get(self, voice_id: str) -> Voice:
        """Voz por id; ValueError si no está en voices.yaml."""
        for entry in self.voices:
            if entry.id == voice_id:
                return entry.to_voice()
        raise ValueError(f"Voz desconocida (no está en voices.yaml): {voice_id!r}")

    def by_role(self, role: str) -> list[Voice]:
        """Voces con ese rol, en el orden del archivo."""
        return [e.to_voice() for e in self.voices if e.role == role]


class ProducerSettings(BaseModel):
    """Configuración de un productor (§5): activo, target_stock, cron y parámetros."""
    model_config = ConfigDict(extra="forbid")

    active: bool = False
    target_stock: int = Field(default=0, ge=0)   # segmentos ready que quiere mantener
    cron: str | None = None                      # 5 campos, hora local; None = solo manual
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, v: str | None) -> str | None:
        if v is not None:
            parse_cron(v)
        return v


class ProducersConfig(BaseModel):
    """Mapa nombre de productor → configuración (producers.yaml)."""
    producers: dict[str, ProducerSettings] = Field(default_factory=dict)

    def get(self, name: str) -> ProducerSettings | None:
        return self.producers.get(name)


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

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def data_dir(self) -> Path:
        """Directorio de datos (station.yaml → data_dir)."""
        return Path(self.station.data_dir)

    @classmethod
    def load(cls, config_dir: Path = Path("config")) -> RadioConfig:
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
