"""
Tests unitarios del framework de producers y del producer de señal horaria
(sin red: FakeLLM / FakeTTS / FakeClock).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import ProducersConfig, ProducerSettings, RadioConfig
from radio.core.models import AudioInfo, SegmentKind, Voice
from radio.core.store import DB
from radio.producers import (
    ProducerContext,
    ProducerRunner,
    TimeSignalProducer,
    pick_voice,
    producer_kind,
    write_segment_audio,
)
from radio.producers.time_signal import hour_phrase, time_signal_text
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.fake import FakeTTS

REPO = Path(__file__).parents[2]
MADRID = ZoneInfo("Europe/Madrid")
UTC = ZoneInfo("UTC")


def make_config(**producers: ProducerSettings) -> RadioConfig:
    base = RadioConfig.load(REPO / "config")
    if not producers:
        return base
    return base.model_copy(update={"producers": ProducersConfig(producers=producers)})


def make_ctx(
    tmp_path: Path,
    *,
    now: datetime | None = None,
    config: RadioConfig | None = None,
) -> ProducerContext:
    return ProducerContext(
        db=DB(":memory:"),
        clock=FakeClock(now or datetime(2026, 9, 24, 16, 20, tzinfo=MADRID)),
        llm=FakeLLM(),
        tts=FakeTTS(),
        config=config or make_config(),
        data_dir=tmp_path,
        prompts_dir=REPO / "prompts",
    )


def host(ctx: ProducerContext) -> Voice:
    return pick_voice(ctx.config, "locutor_principal")


class DummyProducer:
    """Producer mínimo para probar el runner."""

    kind: SegmentKind = "jingle"
    factual = False

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.runs = 0

    def run(self, ctx: ProducerContext) -> list[str]:
        self.runs += 1
        if self.fail:
            raise RuntimeError("boom")
        return [f"{self.name}-{self.runs}"]


# ── Helpers de base ───────────────────────────────────────────────────────────

def test_write_segment_audio_is_atomic(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    info = write_segment_audio(ctx, kind="time_signal", seg_id="X1", text="Hola", voice=host(ctx))
    assert info.path == tmp_path / "stock" / "time_signal" / "X1.wav"
    assert info.path.exists()
    assert info.duration_s > 0
    assert [p.name for p in info.path.parent.iterdir()] == ["X1.wav"]
    assert list((tmp_path / "tmp").iterdir()) == []
    assert isinstance(ctx.tts, FakeTTS)
    call = ctx.tts.calls[0]
    assert call["voice"].id == "locutor_principal"
    assert call["out_path"].parent == tmp_path / "tmp"


def test_write_segment_audio_cleans_tmp_on_failure(tmp_path: Path) -> None:
    class BrokenTTS(FakeTTS):
        def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"partial")
            raise OSError("disk")

    ctx = make_ctx(tmp_path)
    ctx.tts = BrokenTTS()
    with pytest.raises(OSError):
        write_segment_audio(ctx, kind="jingle", seg_id="X2", text="Hola", voice=host(ctx))
    assert list((tmp_path / "tmp").iterdir()) == []
    assert not (tmp_path / "stock" / "jingle").exists()


def test_pick_voice() -> None:
    cfg = make_config()
    voice = pick_voice(cfg, "locutor_principal")
    assert isinstance(voice, Voice) and voice.consent
    with pytest.raises(ValueError):
        pick_voice(cfg, "nope")


def test_producer_kind() -> None:
    assert producer_kind("music_tinydesk") == "music"
    assert producer_kind("time_signal") == "time_signal"
    assert producer_kind("weather") == "weather"


# ── Runner ────────────────────────────────────────────────────────────────────

def test_runner_due_respects_active_and_cron(tmp_path: Path) -> None:
    cfg = make_config(
        a=ProducerSettings(active=True, cron="*/30 * * * *"),
        b=ProducerSettings(active=False, cron="* * * * *"),
        manual=ProducerSettings(active=True, cron=None),
        every=ProducerSettings(active=True, cron="* * * * *"),
    )
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 16, 20, tzinfo=MADRID), config=cfg)
    a, b, manual, every, unknown = (
        DummyProducer(n) for n in ("a", "b", "manual", "every", "unknown")
    )
    runner = ProducerRunner(ctx, [a, b, manual, every, unknown])

    # Primera vez: toca todo lo activo con cron
    assert {p.name for p in runner.due()} == {"a", "every"}
    assert runner.tick() == {"a": ["a-1"], "every": ["every-1"]}

    # Recién ejecutado: nada hasta el siguiente minuto / disparo
    assert runner.due() == []
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(60)
    assert {p.name for p in runner.due()} == {"every"}
    ctx.clock.advance(9 * 60)       # 16:30 → dispara */30
    assert {p.name for p in runner.due()} == {"a", "every"}
    assert b.runs == 0 and manual.runs == 0 and unknown.runs == 0


def test_runner_isolates_errors_and_logs_runs(tmp_path: Path) -> None:
    cfg = make_config(
        bad=ProducerSettings(active=True, cron="*/10 * * * *"),
        good=ProducerSettings(active=True, cron="*/10 * * * *"),
    )
    ctx = make_ctx(tmp_path, config=cfg)
    bad, good = DummyProducer("bad", fail=True), DummyProducer("good")
    result = ProducerRunner(ctx, [bad, good]).tick()

    assert result == {"bad": [], "good": ["good-1"]}
    runs = {r.producer: r for r in ctx.db.list_producer_runs()}
    assert runs["bad"].ok is False
    assert "boom" in str(runs["bad"].error)
    assert runs["good"].ok is True and runs["good"].n_segments == 1
    assert runs["good"].started_at == ctx.clock.now()
    assert runs["good"].ended_at is not None


# ── Señal horaria ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "Son las doce de la noche"),
        (1, "Es la una en punto"),
        (9, "Son las nueve en punto"),
        (12, "Son las doce del mediodía"),
        (13, "Es la una en punto"),
        (21, "Son las nueve en punto"),
    ],
)
def test_hour_phrase(hour: int, expected: str) -> None:
    assert hour_phrase(hour) == expected


def test_time_signal_text_uses_station_name() -> None:
    assert time_signal_text(9, "Radio Parra") == "Son las nueve en punto en Radio Parra."
    assert time_signal_text(13, "Onda Parra") == "Es la una en punto en Onda Parra."


def test_time_signal_creates_stock_without_duplicates(tmp_path: Path) -> None:
    now = datetime(2026, 9, 24, 23, 10, tzinfo=MADRID)
    ctx = make_ctx(tmp_path, now=now)
    producer = TimeSignalProducer()

    ids = producer.run(ctx)
    assert len(ids) == 2
    segs = {s.tags[0]: s for s in ctx.db.list_segments(kind="time_signal")}
    assert set(segs) == {"hour:2026-09-25T00", "hour:2026-09-25T01"}
    midnight = segs["hour:2026-09-25T00"]
    assert midnight.status == "ready"
    assert midnight.factual is True
    assert midnight.priority == 1
    assert midnight.meta["script"] == "Son las doce de la noche en Radio Parra."
    assert midnight.meta["sources"][0]["id"] == "reloj"
    assert midnight.title == "Señal horaria 00:00"
    assert midnight.voice_id == "locutor_principal"
    assert midnight.producer == "time_signal"
    assert midnight.created_at == now
    assert midnight.expires_at == datetime(2026, 9, 25, 0, 5, tzinfo=MADRID)
    assert midnight.path.exists()
    assert midnight.path.parent == tmp_path / "stock" / "time_signal"
    assert segs["hour:2026-09-25T01"].meta["script"] == "Es la una en punto en Radio Parra."

    # Segunda ejecución inmediata: nada nuevo
    assert producer.run(ctx) == []
    assert len(ctx.db.list_segments(kind="time_signal")) == 2

    # Una hora después solo falta la siguiente
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(3600)
    assert len(producer.run(ctx)) == 1
    assert len(ctx.db.list_segments(kind="time_signal")) == 3


def test_time_signal_hours_ahead_follow_target_stock(tmp_path: Path) -> None:
    cfg = make_config(time_signal=ProducerSettings(active=True, target_stock=4, cron="0 * * * *"))
    ctx = make_ctx(tmp_path, config=cfg)
    assert len(TimeSignalProducer().run(ctx)) == 4


def test_time_signal_expires_past_hours(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 8, 30, tzinfo=MADRID))
    producer = TimeSignalProducer()
    producer.run(ctx)  # crea 09:00 y 10:00

    def statuses() -> dict[str, str]:
        return {s.tags[0]: s.status for s in ctx.db.list_segments(kind="time_signal")}

    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(34 * 60)  # 09:04 → la de las 09 sigue en ventana
    producer.run(ctx)
    assert statuses()["hour:2026-09-24T09"] == "ready"
    assert ctx.db.stock_view(ctx.clock.now()).count("time_signal") == 3

    ctx.clock.advance(60)  # 09:05 → fuera de ventana: caduca
    producer.run(ctx)
    by_tag = statuses()
    assert by_tag["hour:2026-09-24T09"] == "expired"
    assert by_tag["hour:2026-09-24T10"] == "ready"
    assert by_tag["hour:2026-09-24T11"] == "ready"
    # La caducada no se vuelve a crear
    assert len(by_tag) == 3


def test_time_signal_regenerates_quarantined(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 8, 30, tzinfo=MADRID))
    producer = TimeSignalProducer()
    first = producer.run(ctx)
    ctx.db.update_segment_status(first[0], "quarantined")
    again = producer.run(ctx)
    assert len(again) == 1
    new = ctx.db.get_segment(again[0])
    assert new is not None and new.tags == ("hour:2026-09-24T09",)


def test_time_signal_uses_station_timezone(tmp_path: Path) -> None:
    # 22:30 UTC = 00:30 en Madrid (verano)
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 22, 30, tzinfo=UTC))
    TimeSignalProducer().run(ctx)
    tags = {s.tags[0] for s in ctx.db.list_segments(kind="time_signal")}
    assert tags == {"hour:2026-09-25T01", "hour:2026-09-25T02"}
    seg = ctx.db.find_by_meta("time_signal", "tags", "hour:2026-09-25T01")
    assert seg is not None and seg.expires_at is not None
    assert seg.expires_at - timedelta(minutes=5) == datetime(2026, 9, 25, 1, tzinfo=MADRID)
