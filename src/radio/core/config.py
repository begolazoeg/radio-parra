"""
Configuración tipada de Radio Parra usando Pydantic v2.
Carga los 4 archivos YAML desde config/ sin requerir .env.
"""

from __future__ import annotations

import re
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


class AudioConfig(BaseModel):
    """Salida de audio de la emisora (§10: lo único que cambia entre portátil y Pi)."""
    model_config = ConfigDict(extra="forbid")

    mpv_bin: str = "mpv"
    # Argumentos extra para mpv, p. ej. ["--audio-device=alsa/plughw:CARD=Device"]
    mpv_args: list[str] = Field(default_factory=list)


class InterruptsConfig(BaseModel):
    """Cómo da paso la emisora a una interrupción de la parrilla (señal horaria)."""
    model_config = ConfigDict(extra="forbid")

    # True: se corta la música en curso a la hora exacta (§4.3: "la emisora puede
    # cortar lo que suene"). False: la interrupción espera a que acabe el archivo en
    # curso (si así llega tarde, más allá de max_late_seconds, se omite).
    cut_music: StrictBool = True


class PlayoutConfig(BaseModel):
    """Cola de la emisora (§4.4) y bucle de emergencia (§8, peldaño 5)."""
    model_config = ConfigDict(extra="forbid")

    # Unidades encoladas por detrás de lo que suena (§4.4: lookahead de 2-3)
    lookahead_units: int = Field(default=2, ge=1, le=5)
    emergency_dir: str = "assets/emergency"
    # Segundos hasta volver a intentar programar tras caer al peldaño 5
    emergency_retry_s: float = Field(default=30.0, gt=0)


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
    audio: AudioConfig = AudioConfig()
    interrupts: InterruptsConfig = InterruptsConfig()
    playout: PlayoutConfig = PlayoutConfig()


# ── Parrilla (grid.yaml, §5) ──────────────────────────────────────────────────

# Tipos de hueco permitidos en ``pattern`` (ver radio.grid.scheduler)
SLOT_KINDS: frozenset[str] = frozenset({"music", "talk", "jingle"})

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _check_hhmm(value: str, *, allow_24: bool) -> str:
    """Valida "HH:MM" (00:00–23:59); ``allow_24`` acepta además "24:00"."""
    v = value.strip()
    if allow_24 and v == "24:00":
        return v
    if not _HHMM.match(v):
        raise ValueError(f"hora inválida {value!r}: se espera 'HH:MM'")
    return v


class TalkBudget(BaseModel):
    """Presupuesto de charla (invariante §1.8): tope de palabra en ventana móvil."""
    model_config = ConfigDict(extra="forbid")

    window_minutes: int = Field(default=60, gt=0)
    max_ratio: float = Field(default=0.22, ge=0.0, le=1.0)


class InterruptRule(BaseModel):
    """
    Regla de interrupción de un modo: emitir ``kind`` cuando se cumpla ``when``
    (p. ej. ``"minute == 0"``), con un retraso máximo de ``max_late_seconds``.
    """
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    when: str
    max_late_seconds: float = Field(default=90.0, ge=0.0)

    @field_validator("when")
    @classmethod
    def valid_when(cls, v: str) -> str:
        # Importación diferida: radio.grid importa este módulo
        from radio.grid.rules import parse_when  # noqa: PLC0415

        parse_when(v)
        return v


class Daypart(BaseModel):
    """
    Franja horaria de un modo (hora local, ``from`` incluido, ``to`` excluido).
    Si ``to`` < ``from`` cruza la medianoche; ``from == to`` o "00:00"–"24:00" = todo el día.
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    from_: str = Field(alias="from")
    to: str
    pattern: list[str] = Field(min_length=1)
    talk_pool: dict[str, float] = Field(default_factory=dict)

    @field_validator("from_")
    @classmethod
    def valid_from(cls, v: str) -> str:
        return _check_hhmm(v, allow_24=False)

    @field_validator("to")
    @classmethod
    def valid_to(cls, v: str) -> str:
        return _check_hhmm(v, allow_24=True)

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - SLOT_KINDS)
        if unknown:
            raise ValueError(f"huecos desconocidos en pattern: {unknown} (válidos: {sorted(SLOT_KINDS)})")
        return v

    @field_validator("talk_pool")
    @classmethod
    def valid_pool(cls, v: dict[str, float]) -> dict[str, float]:
        bad = sorted(k for k, w in v.items() if w < 0)
        if bad:
            raise ValueError(f"pesos negativos en talk_pool: {bad}")
        return v


class ModeConfig(BaseModel):
    """Un modo ("emisora", §4.3): interrupciones y franjas horarias."""
    model_config = ConfigDict(extra="forbid")

    interrupts: list[InterruptRule] = Field(default_factory=list)
    dayparts: list[Daypart] = Field(default_factory=list)


def _default_modes() -> dict[str, ModeConfig]:
    """Sin grid.yaml: un único modo ``default`` de solo música todo el día."""
    return {
        "default": ModeConfig(
            dayparts=[Daypart.model_validate(
                {"name": "todo", "from": "00:00", "to": "24:00", "pattern": ["music"]}
            )]
        )
    }


class GridConfig(BaseModel):
    """Configuración de la parrilla (grid.yaml, §5). La interpreta ``radio.grid``."""
    model_config = ConfigDict(extra="forbid")

    timezone: str = "Europe/Madrid"
    talk_budget: TalkBudget = TalkBudget()
    cooldowns_minutes: dict[str, float] = Field(default_factory=dict)
    modes: dict[str, ModeConfig] = Field(default_factory=_default_modes)

    @field_validator("cooldowns_minutes")
    @classmethod
    def valid_cooldowns(cls, v: dict[str, float]) -> dict[str, float]:
        bad = sorted(k for k, m in v.items() if m < 0)
        if bad:
            raise ValueError(f"cooldowns negativos: {bad}")
        return v

    @field_validator("modes")
    @classmethod
    def modes_not_empty(cls, v: dict[str, ModeConfig]) -> dict[str, ModeConfig]:
        if not v:
            raise ValueError("grid.yaml necesita al menos un modo")
        return v


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
