"""
Producer de intervenciones breves de la locutora IA.

Renderiza los prompts de prompts/host/, pide al LLM un guion en JSON
{"script": "..."}, lo valida y lo sintetiza con la voz principal.
Mantiene un stock acotado de intros listas.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from radio.core.ids import new_id
from radio.core.models import SegmentKind
from radio.producers.base import (
    STATION_NAME,
    ProducerContext,
    pick_voice,
    write_segment_audio,
)

VOICE_ID = "host_main"
# Con este número de intros "ready" ya no se genera ninguna más
MAX_READY = 3
MAX_SCRIPT_CHARS = 600
RECENT_MUSIC = 3

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"script": {"type": "string", "minLength": 1}},
    "required": ["script"],
    "additionalProperties": False,
}

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
# Guarda ligera: la locutora es una IA y no debe afirmar ser humana
_HUMAN_CLAIM_RE = re.compile(
    r"(?<!no )\bsoy\s+(?:un\s+|una\s+)?(?:ser\s+human[oa]|human[oa]|persona(?:\s+real)?)\b",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s|$)")


def time_of_day(hour: int) -> str:
    """Etiqueta genérica de franja del día (independiente de la parrilla)."""
    if 6 <= hour < 14:
        return "mañana"
    if 14 <= hour < 21:
        return "tarde"
    return "noche"


def truncate_at_sentence(text: str, limit: int = MAX_SCRIPT_CHARS) -> str:
    """Recorta `text` a `limit` caracteres, preferiblemente al final de una frase."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(head)]
    if ends:
        return head[: ends[-1]].strip()
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return cut.rstrip(" ,;:") + "…"


def parse_script(raw: str) -> str:
    """
    Extrae y valida el guion de la respuesta del LLM.
    Lanza ValueError si no es JSON válido, está vacío, contiene URLs
    o la locutora afirma ser humana.
    """
    cleaned = _FENCE_RE.sub("", raw.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Respuesta del LLM no es JSON: {raw[:80]!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("script"), str):
        raise ValueError("Respuesta del LLM sin campo 'script' de tipo texto")
    script = " ".join(data["script"].split())
    if not script:
        raise ValueError("Guion vacío")
    if _URL_RE.search(script):
        raise ValueError("El guion contiene URLs")
    if _HUMAN_CLAIM_RE.search(script):
        raise ValueError("El guion afirma que la locutora es humana")
    return truncate_at_sentence(script)


class HostIntroProducer:
    """Genera intervenciones cortas de la locutora entre canciones."""
    name = "host_intro"
    kind: SegmentKind = "host_intro"

    def run(self, ctx: ProducerContext) -> list[str]:
        if len(ctx.db.list_segments(kind=self.kind, status="ready")) >= MAX_READY:
            return []

        tz = ZoneInfo(ctx.config.station.timezone)
        now = ctx.clock.now().astimezone(tz)
        system, user = self._render_prompts(ctx, now)
        result = ctx.llm.complete(
            system,
            user,
            temperature=0.8,
            json_schema=SCRIPT_SCHEMA,
            max_tokens=400,
        )
        script = parse_script(result.text)

        voice = pick_voice(ctx.config, VOICE_ID)
        seg_id = new_id()
        audio = write_segment_audio(
            ctx, kind=self.kind, seg_id=seg_id, text=script, voice_id=voice.id
        )
        ctx.db.add_segment(
            id=seg_id,
            kind=self.kind,
            status="ready",
            created_at=ctx.clock.now().isoformat(),
            title=f"Locutora {now:%H:%M}",
            duration_s=audio.duration_s,
            audio_path=audio.path,
            producer=self.name,
            script=script,
            voice_id=voice.id,
            tags=[f"tod:{time_of_day(now.hour)}"],
        )
        return [seg_id]

    def _render_prompts(self, ctx: ProducerContext, now: datetime) -> tuple[str, str]:
        """Renderiza system.j2 e intro.j2 con el contexto de la emisora."""
        env = Environment(
            loader=FileSystemLoader(str(ctx.prompts_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        plays = [p for p in ctx.db.list_plays() if p.get("kind") == "music"]
        # list_plays viene en orden cronológico; las más recientes primero
        recent = [str(p["title"]) for p in reversed(plays[-RECENT_MUSIC:])]
        variables: dict[str, Any] = {
            "station_name": STATION_NAME,
            "language": ctx.config.station.language,
            "local_time": f"{now:%H:%M}",
            "time_of_day": time_of_day(now.hour),
            "recent_music": recent,
            "max_chars": MAX_SCRIPT_CHARS,
        }
        system = env.get_template("host/system.j2").render(**variables)
        user = env.get_template("host/intro.j2").render(**variables)
        return system, user
