"""
``radio preview <producer>`` (§9): genera **un** segmento y lo reproduce en local, para
iterar prompts y voces. Hoy solo existe ``host_intro`` (Fase 2).

- Sin ``--fake``: proveedores de ``station.yaml`` (Claude + Piper, con su caché de
  TTS) y fuentes abiertas reales (MusicBrainz/Wikipedia). La canción es ``--music-id``
  o la siguiente candidata de ``data/state.db``. **No se registra** en la BD salvo
  ``--register``. Como gasta, respeta la regla de gasto y deja su coste en
  ``producer_runs`` con el nombre ``preview:host_intro`` (cuenta para el presupuesto,
  pero no para el ``cron`` del productor).
- Con ``--fake``: sin red ni claves. BD en memoria con una canción de la artista
  ficticia de ``radio.producers.host_intro_fake``, ``FakeLLM`` con una respuesta
  pegada a las fuentes, ``FakeTTS`` (silencio) y fuentes sintéticas. Nunca registra.

Imprime guion, claims, fuentes (licencia y URL), informe de grounding, intentos y
coste, y reproduce el audio con mpv salvo ``--no-play`` (``--out`` lo guarda).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from radio.core.clock import FakeClock, SystemClock
from radio.core.config import AudioConfig, RadioConfig
from radio.core.models import Segment
from radio.core.paths import db_path
from radio.core.store import DB
from radio.producers.base import (
    BUDGET_EXHAUSTED,
    DraftRejected,
    ProducerContext,
    budget_exhausted,
)
from radio.producers.host_intro import HostIntroProducer, PreviewResult
from radio.producers.host_intro_fake import FAKE_ARTIST, FAKE_TITLE, fake_intro_llm, fake_sources
from radio.producers.post import NullPost
from radio.providers.errors import ProviderError
from radio.providers.tts.fake import FakeTTS

PREVIEWABLE: tuple[str, ...] = ("host_intro",)
PREVIEW_RUN_PREFIX = "preview:"

Echo = Callable[[str], None]
# Reproduce un archivo; devuelve el código de salida (0 = bien)
Player = Callable[[Path, AudioConfig], int]


class PreviewError(RuntimeError):
    """La previsualización no se puede hacer (mensaje para la persona)."""


def play_with_mpv(path: Path, audio: AudioConfig) -> int:
    """Reproduce ``path`` con mpv (bloqueante), con los argumentos de ``station.yaml``."""
    binary = shutil.which(audio.mpv_bin)
    if binary is None:
        raise PreviewError(f"mpv no está instalado ({audio.mpv_bin}); usa --no-play o --out")
    cmd = [binary, "--no-video", "--really-quiet", *audio.mpv_args, str(path)]
    return subprocess.run(cmd, check=False).returncode


def format_result(result: PreviewResult) -> str:
    """Informe legible de una intro previsualizada."""
    meta = result.draft.meta
    g: dict[str, Any] = meta.get("grounding", {})
    lines = [
        f"Canción: {result.music.title} ({result.music.id})",
        f"Artista: {meta.get('artist') or '(desconocido)'}",
        f"Resultado: {g.get('outcome', '?')} — estado {result.draft.status}"
        + (" (con datos)" if g.get("has_facts") else " (sin datos)"),
        "",
        "Guion:",
        f"  {result.draft.script}",
        "",
        f"Claims ({len(meta.get('claims', []))}):",
    ]
    lines += [f"  - «{c['text']}» → {c['source_id']}" for c in meta.get("claims", [])]
    lines.append(f"Fuentes usadas ({len(meta.get('sources', []))}):")
    lines += [f"  - {s['id']} [{s.get('license') or 'sin licencia'}] {s.get('url')}"
              for s in meta.get("sources", [])]
    gathered = meta.get("gathered_sources", [])
    if gathered and not meta.get("sources"):
        lines.append(f"  (recogidas pero no usadas: {', '.join(gathered)})")
    lines.append("Grounding: " + ("OK" if g.get("ok") else "FALLO"))
    lines += [f"  - {p}" for p in g.get("problems", [])]
    lines.append(f"Intentos: {g.get('attempts', 0)}")
    for n, att in enumerate(meta.get("attempts", []), start=1):
        mark = "OK" if not att["problems"] else f"{len(att['problems'])} problemas"
        lines.append(f"  {n}. {att['variant']}{' (estricto)' if att['strict'] else ''}: {mark}")
        lines += [f"     - {p}" for p in att["problems"]]
    duration = result.draft.audio.duration_s if result.draft.audio else 0.0
    lines += [
        "",
        f"Modelo: {meta.get('model') or '?'} · prompt {result.draft.prompt_version}",
        f"Coste: {result.cost_eur:.4f} € · tokens {result.tokens_in} entrada / "
        f"{result.tokens_out} salida · TTS {result.tts_chars} caracteres",
        f"Audio: {result.audio_path or '(ninguno)'} ({duration:.1f} s)",
    ]
    if result.segment is not None:
        lines.append(f"Registrado: {result.segment.id} ({result.segment.status})")
    return "\n".join(lines)


def _finish(
    result: PreviewResult, *, config: RadioConfig, play: bool, out: Path | None,
    player: Player, echo: Echo, keep: bool,
) -> int:
    echo(format_result(result))
    code = 0
    if play and result.audio_path is not None:
        echo("\nReproduciendo…")
        code = player(result.audio_path, config.station.audio)
        if code != 0:
            echo(f"mpv terminó con código {code}")
    if not keep and out is None and result.audio_path is not None:
        result.audio_path.unlink(missing_ok=True)
    return 0 if code == 0 else 1


def preview_fake(
    config: RadioConfig,
    *,
    prompts_dir: Path,
    play: bool = True,
    out: Path | None = None,
    player: Player = play_with_mpv,
    echo: Echo = print,
) -> int:
    """``radio preview host_intro --fake``: todo con dobles, en memoria."""
    with tempfile.TemporaryDirectory(prefix="radio-preview-") as tmp:
        data = Path(tmp)
        clock = SystemClock(config.station.timezone)
        now = clock.now()
        db = DB(":memory:")
        try:
            music_path = data / "stock" / "music" / "fake.mp3"
            music_path.parent.mkdir(parents=True)
            music_path.write_bytes(b"")
            music = Segment(
                id="preview-music", kind="music", factual=False, path=music_path,
                duration_s=900.0, created_at=now - timedelta(minutes=1),
                producer="preview", meta={"title": FAKE_TITLE, "artist": FAKE_ARTIST},
            )
            db.add_segment(music)
            ctx = ProducerContext(
                db=db, clock=FakeClock(now), llm=fake_intro_llm(), tts=FakeTTS(),
                config=config, data_dir=data, prompts_dir=prompts_dir, post=NullPost(),
            )
            producer = HostIntroProducer(config, source_gatherer=fake_sources)
            try:
                result = producer.preview(ctx, music, out_path=out)
            except DraftRejected as exc:
                raise PreviewError(f"no se pudo escribir la intro: {exc}") from exc
            echo("(modo --fake: LLM, TTS y fuentes simulados; nada se registra)\n")
            return _finish(result, config=config, play=play, out=out, player=player,
                           echo=echo, keep=False)
        finally:
            db.close()


def preview_real(
    config: RadioConfig,
    *,
    data_dir: Path,
    prompts_dir: Path,
    music_id: str | None = None,
    play: bool = True,
    out: Path | None = None,
    register: bool = False,
    ctx: ProducerContext | None = None,
    http_client: httpx.Client | None = None,
    player: Player = play_with_mpv,
    echo: Echo = print,
) -> int:
    """
    ``radio preview host_intro``: proveedores y fuentes reales sobre ``data/state.db``.
    ``ctx`` y ``http_client`` permiten inyectar dobles (tests).
    """
    from radio.producers.runner import build_context  # noqa: PLC0415

    path = db_path(data_dir)
    if ctx is None and not path.exists():
        raise PreviewError(f"No existe la BD {path}: ejecuta `radio produce music_tinydesk` "
                           "o usa --fake")
    own_db = ctx is None
    db = DB(path) if ctx is None else ctx.db
    try:
        if ctx is None:
            ctx = build_context(config, db, SystemClock(config.station.timezone), data_dir,
                                prompts_dir=prompts_dir)
        now = ctx.clock.now()
        if budget_exhausted(db, config, now):
            raise PreviewError(f"{BUDGET_EXHAUSTED}: no se previsualiza con proveedores de pago")
        music = None
        if music_id is not None:
            music = db.get_segment(music_id)
            if music is None or music.kind != "music":
                raise PreviewError(f"No hay ninguna canción con id {music_id!r}")
        producer = HostIntroProducer(config, client=http_client)
        run_ctx = ctx.for_run()
        run_id = db.start_producer_run(f"{PREVIEW_RUN_PREFIX}{producer.name}", now)
        error: str | None = None
        try:
            result = producer.preview(run_ctx, music, register=register, out_path=out)
        except LookupError as exc:
            error = str(exc)
            raise PreviewError(f"{exc} (usa --music-id o --fake)") from exc
        except (DraftRejected, ProviderError) as exc:
            error = str(exc)
            raise PreviewError(f"no se pudo generar la intro: {exc}") from exc
        finally:
            s = run_ctx.stats
            db.finish_producer_run(
                run_id, ended_at=ctx.clock.now(), ok=error is None,
                n_segments=s.n_segments, tokens_in=s.tokens_in, tokens_out=s.tokens_out,
                tts_chars=s.tts_chars, cost_eur=s.cost_eur, error=error,
            )
        return _finish(result, config=config, play=play, out=out, player=player, echo=echo,
                       keep=register)
    finally:
        if own_db:
            db.close()
