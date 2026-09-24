"""
Producer de señal horaria.

Mantiene un pequeño stock de locuciones "Son las X en punto" para las
próximas horas en punto (hora local de la emisora). No usa LLM, solo TTS.

- Número de horas preparadas: ``target_stock`` de producers.yaml (por defecto 2).
- Cada señal es factual (la fuente es el reloj), con prioridad alta (1, §14) y
  caduca ``SIGNAL_WINDOW`` después de su hora en punto: pasada la ventana ya no
  tiene sentido emitirla. Las caducadas se marcan ``expired`` en cada ejecución.
- Se identifica por la etiqueta ``hour:YYYY-MM-DDTHH`` en ``meta["tags"]``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from radio.core.ids import new_id
from radio.core.models import Segment, SegmentKind
from radio.grid.rules import TIME_SIGNAL_WINDOW_MIN
from radio.producers.base import ProducerContext, pick_voice, write_segment_audio

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


class TimeSignalProducer:
    """Genera las señales horarias de las próximas horas."""
    name = "time_signal"
    kind: SegmentKind = "time_signal"
    factual = True

    def run(self, ctx: ProducerContext) -> list[str]:
        tz = ZoneInfo(ctx.config.station.timezone)
        created_at = ctx.clock.now()
        now = created_at.astimezone(tz)

        # 1) Caducar señales cuya ventana ya pasó
        ctx.db.expire_segments(now, kind=self.kind)

        # 2) Crear las que falten para las próximas horas en punto
        #    (una en cuarentena, p. ej. por audio perdido, se vuelve a generar)
        have = {
            tag
            for seg in ctx.db.list_segments(kind=self.kind)
            if seg.status != "quarantined"
            for tag in seg.tags
            if tag.startswith(TAG_PREFIX)
        }
        settings = ctx.config.producers.get(self.name)
        hours_ahead = settings.target_stock if settings and settings.target_stock else HOURS_AHEAD
        voice = pick_voice(ctx.config, VOICE_ID)
        station_name = ctx.config.station.name
        base = now.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        created: list[str] = []
        for i in range(1, hours_ahead + 1):
            # Aritmética en UTC para respetar los cambios de horario
            slot = (base + timedelta(hours=i)).astimezone(tz)
            tag = hour_tag(slot)
            if tag in have:
                continue
            text = time_signal_text(slot.hour, station_name)
            seg_id = new_id()
            audio = write_segment_audio(
                ctx, kind=self.kind, seg_id=seg_id, text=text, voice=voice
            )
            ctx.db.add_segment(
                Segment(
                    id=seg_id,
                    kind=self.kind,
                    factual=self.factual,
                    path=audio.path,
                    duration_s=audio.duration_s,
                    created_at=created_at,
                    producer=self.name,
                    expires_at=slot + SIGNAL_WINDOW,
                    priority=PRIORITY,
                    voice_id=voice.id,
                    meta={
                        "title": f"Señal horaria {slot:%H}:00",
                        "tags": [tag],
                        "script": text,
                        "sources": [{"id": "reloj", "text": slot.isoformat(), "url": ""}],
                    },
                )
            )
            have.add(tag)
            created.append(seg_id)
        return created
