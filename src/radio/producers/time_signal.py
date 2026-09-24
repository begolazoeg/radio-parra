"""
Productor de señal horaria (``time_signal``).

Mantiene un pequeño stock de locuciones "Son las X en punto" para las próximas
horas en punto (hora local de la emisora). No usa LLM, solo TTS.

- Horas preparadas: ``target_stock`` de producers.yaml (por defecto 2).
- ``deficit``: cuántas de esas próximas horas no tienen señal emitible en el stock.
- Cada señal es factual (la fuente es el reloj), con prioridad alta (1, §14) y
  caduca ``SIGNAL_WINDOW`` después de su hora en punto. Las caducadas se marcan
  ``expired`` al empezar cada ejecución (``prepare``).
- Se identifica por la etiqueta ``hour:YYYY-MM-DDTHH`` en ``meta["tags"]``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from radio.core.models import SegmentKind, SourceDoc, StockView
from radio.grid.rules import TIME_SIGNAL_WINDOW_MIN
from radio.producers.base import Draft, ProducerContext, StagedProducer, pick_voice

# Número de horas futuras con señal preparada si producers.yaml no dice otra cosa
HOURS_AHEAD = 2
# Pasado este margen tras la hora en punto, la señal caduca
SIGNAL_WINDOW = timedelta(minutes=TIME_SIGNAL_WINDOW_MIN)
# Prioridad "alta" del catálogo (§14): >0 puede interrumpir
PRIORITY = 1
VOICE_ID = "locutor_principal"
TAG_PREFIX = "hour:"
_TAG_FORMAT = "%Y-%m-%dT%H"

_HOUR_WORDS = [
    "doce", "una", "dos", "tres", "cuatro", "cinco",
    "seis", "siete", "ocho", "nueve", "diez", "once",
]


def hour_phrase(hour: int) -> str:
    """Hora en punto en palabras (formato 12h): 'Es la una en punto', 'Son las doce de la noche'..."""
    if not 0 <= hour <= 23:
        raise ValueError(f"Hora fuera de rango: {hour}")
    if hour == 0:
        return "Son las doce de la noche"
    if hour == 12:
        return "Son las doce del mediodía"
    word = _HOUR_WORDS[hour % 12]
    if hour % 12 == 1:
        return f"Es la {word} en punto"
    return f"Son las {word} en punto"


def time_signal_text(hour: int, station_name: str) -> str:
    """Texto completo de la señal horaria."""
    return f"{hour_phrase(hour)} en {station_name}."


def hour_tag(dt: datetime) -> str:
    """Etiqueta 'hour:YYYY-MM-DDTHH' en hora local."""
    return TAG_PREFIX + dt.strftime(_TAG_FORMAT)


class TimeSignalProducer(StagedProducer):
    """Genera las señales horarias de las próximas horas."""
    name = "time_signal"
    kind: SegmentKind = "time_signal"
    factual = True
    default_target_stock = HOURS_AHEAD

    def upcoming_slots(self, now: datetime) -> list[datetime]:
        """Las próximas ``target_stock`` horas en punto (hora local)."""
        # Aritmética en UTC para respetar los cambios de horario
        base = now.astimezone(self.tz).replace(minute=0, second=0, microsecond=0)
        base_utc = base.astimezone(UTC)
        return [
            (base_utc + timedelta(hours=i)).astimezone(self.tz)
            for i in range(1, self.target_stock + 1)
        ]

    def deficit(self, stock: StockView, now: datetime) -> int:
        have = {t for seg in stock.get(self.kind) for t in seg.tags}
        return sum(1 for slot in self.upcoming_slots(now) if hour_tag(slot) not in have)

    def prepare(self, ctx: ProducerContext, now: datetime) -> None:
        """Caduca las señales cuya ventana ya pasó."""
        ctx.db.expire_segments(now, kind=self.kind)

    def gather(self, ctx: ProducerContext, wanted: int) -> list[Draft]:
        """
        Un borrador por hora sin señal. Una en cuarentena (p. ej. audio perdido) se
        vuelve a generar; cualquier otro estado cuenta como existente.
        """
        if wanted <= 0:
            return []
        now = ctx.clock.now()
        have = {
            tag
            for seg in ctx.db.list_segments(kind=self.kind)
            if seg.status != "quarantined"
            for tag in seg.tags
            if tag.startswith(TAG_PREFIX)
        }
        voice = pick_voice(ctx.config, VOICE_ID)
        drafts: list[Draft] = []
        for slot in self.upcoming_slots(now):
            tag = hour_tag(slot)
            if tag in have:
                continue
            drafts.append(Draft(
                sources=[SourceDoc(id="reloj", text=slot.isoformat(), url="")],
                voice=voice,
                expires_at=slot + SIGNAL_WINDOW,
                priority=PRIORITY,
                meta={"title": f"Señal horaria {slot:%H}:00", "tags": [tag], "hour": slot.hour},
            ))
        return drafts

    def write(self, ctx: ProducerContext, draft: Draft) -> Draft:
        """Plantilla fija: la hora sale del reloj, no de un LLM."""
        draft.script = time_signal_text(int(draft.meta.pop("hour")), ctx.config.station.name)
        return draft
