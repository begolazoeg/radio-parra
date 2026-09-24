"""
Producer de señal horaria.

Mantiene un pequeño stock de locuciones "Son las X en punto" para las
próximas horas en punto (hora local de la emisora). No usa LLM, solo TTS.
Las señales de horas ya pasadas se marcan como "done" para no emitirlas tarde.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from radio.core.ids import new_id
from radio.core.models import SegmentKind
from radio.producers.base import (
    STATION_NAME,
    ProducerContext,
    pick_voice,
    write_segment_audio,
)

# Número de horas futuras con señal preparada
HOURS_AHEAD = 2
# Una señal cuya hora pasó hace más de esto ya no se emite
EXPIRY = timedelta(hours=1)
VOICE_ID = "host_main"
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


def time_signal_text(hour: int) -> str:
    """Texto completo de la señal horaria."""
    return f"{hour_phrase(hour)} en {STATION_NAME}."


def hour_tag(dt: datetime) -> str:
    """Etiqueta 'hour:YYYY-MM-DDTHH' en hora local."""
    return TAG_PREFIX + dt.strftime(_TAG_FORMAT)


class TimeSignalProducer:
    """Genera las señales horarias de las próximas horas."""
    name = "time_signal"
    kind: SegmentKind = "time_signal"

    def run(self, ctx: ProducerContext) -> list[str]:
        tz = ZoneInfo(ctx.config.station.timezone)
        now = ctx.clock.now().astimezone(tz)
        existing = ctx.db.list_segments(kind=self.kind)

        # 1) Caducar señales de horas ya pasadas
        for seg in existing:
            if seg["status"] != "ready":
                continue
            slot = _tag_hour(seg["tags"], tz)
            if slot is not None and slot < now - EXPIRY:
                ctx.db.update_segment_status(seg["id"], "done")

        # 2) Crear las que falten para las próximas horas en punto
        have = {
            tag
            for seg in existing
            if seg["status"] != "error"
            for tag in seg["tags"]
            if tag.startswith(TAG_PREFIX)
        }
        voice = pick_voice(ctx.config, VOICE_ID)
        base = now.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        created: list[str] = []
        for i in range(1, HOURS_AHEAD + 1):
            # Aritmética en UTC para respetar los cambios de horario
            slot = (base + timedelta(hours=i)).astimezone(tz)
            tag = hour_tag(slot)
            if tag in have:
                continue
            text = time_signal_text(slot.hour)
            seg_id = new_id()
            audio = write_segment_audio(
                ctx, kind=self.kind, seg_id=seg_id, text=text, voice_id=voice.id
            )
            ctx.db.add_segment(
                id=seg_id,
                kind=self.kind,
                status="ready",
                created_at=ctx.clock.now().isoformat(),
                title=f"Señal horaria {slot:%H}:00",
                duration_s=audio.duration_s,
                audio_path=audio.path,
                producer=self.name,
                script=text,
                voice_id=voice.id,
                tags=[tag],
            )
            have.add(tag)
            created.append(seg_id)
        return created


def _tag_hour(tags: list[str], tz: ZoneInfo) -> datetime | None:
    """Extrae la hora local de la etiqueta 'hour:...' o None si no la hay."""
    for tag in tags:
        if tag.startswith(TAG_PREFIX):
            try:
                naive = datetime.strptime(tag[len(TAG_PREFIX):], _TAG_FORMAT)
            except ValueError:
                return None
            return naive.replace(tzinfo=tz)
    return None
