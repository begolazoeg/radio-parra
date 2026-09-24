"""
Tests unitarios del framework de producers y de los producers de
señal horaria y locutora (sin red: FakeLLM / FakeTTS / FakeClock).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import ProducersConfig, ProducerSettings, RadioConfig
from radio.core.models import SegmentKind
from radio.core.store import DB
from radio.producers import (
    HostIntroProducer,
    ProducerContext,
    ProducerRunner,
    TimeSignalProducer,
    pick_voice,
    write_segment_audio,
)
from radio.producers.host_intro import parse_script, truncate_at_sentence
from radio.producers.time_signal import hour_phrase, time_signal_text
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.fake import FakeTTS

REPO = Path(__file__).parents[2]
MADRID = ZoneInfo("Europe/Madrid")
GOOD_SCRIPT = "Buenas tardes, esto es Radio Parra. Seguimos con más música en directo."


def make_config(**producers: ProducerSettings) -> RadioConfig:
    base = RadioConfig.load(REPO / "config")
    return base.model_copy(update={"producers": ProducersConfig(producers=producers)})


def make_ctx(
    tmp_path: Path,
    *,
    now: datetime | None = None,
    fixture: object = None,
    config: RadioConfig | None = None,
) -> ProducerContext:
    return ProducerContext(
        db=DB(":memory:"),
        clock=FakeClock(now or datetime(2026, 9, 24, 16, 20, tzinfo=MADRID)),
        llm=FakeLLM({"script": GOOD_SCRIPT} if fixture is None else fixture),
        tts=FakeTTS(),
        config=config or make_config(),
        data_dir=tmp_path,
        prompts_dir=REPO / "prompts",
    )


class DummyProducer:
    """Producer mínimo para probar el runner."""

    kind: SegmentKind = "jingle"

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.runs = 0

    def run(self, ctx: ProducerContext) -> list[str]:
        self.runs += 1
        if self.fail:
            raise RuntimeError("boom")
        return [f"{self.name}-{self.runs}"]


def producer_runs(db: DB) -> list[dict[str, object]]:
    return [dict(r) for r in db._conn.execute("SELECT * FROM producer_runs ORDER BY id")]


# ── Helpers de base ───────────────────────────────────────────────────────────

def test_write_segment_audio_is_atomic(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    info = write_segment_audio(ctx, kind="host_intro", seg_id="X1", text="Hola", voice_id="host_main")
    assert info.path == tmp_path / "segments" / "host_intro" / "X1.wav"
    assert info.path.exists()
    assert info.duration_s > 0
    assert [p.name for p in info.path.parent.iterdir()] == ["X1.wav"]


def test_write_segment_audio_cleans_tmp_on_failure(tmp_path: Path) -> None:
    class BrokenTTS(FakeTTS):
        def synthesize(self, text: str, voice: str, out_path: Path):  # type: ignore[no-untyped-def]
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"partial")
            raise OSError("disk")

    ctx = make_ctx(tmp_path)
    ctx.tts = BrokenTTS()
    with pytest.raises(OSError):
        write_segment_audio(ctx, kind="jingle", seg_id="X2", text="Hola", voice_id="host_main")
    assert list((tmp_path / "segments" / "jingle").iterdir()) == []


def test_pick_voice() -> None:
    cfg = make_config()
    assert pick_voice(cfg, "host_main").id == "host_main"
    with pytest.raises(ValueError):
        pick_voice(cfg, "nope")


# ── Runner ────────────────────────────────────────────────────────────────────

def test_runner_due_respects_active_and_interval(tmp_path: Path) -> None:
    cfg = make_config(
        a=ProducerSettings(active=True, interval_minutes=30),
        b=ProducerSettings(active=False, interval_minutes=0),
        every=ProducerSettings(active=True, interval_minutes=0),
    )
    ctx = make_ctx(tmp_path, config=cfg)
    a, b, every, unknown = (DummyProducer(n) for n in ("a", "b", "every", "unknown"))
    runner = ProducerRunner(ctx, [a, b, every, unknown])

    assert {p.name for p in runner.due()} == {"a", "every"}
    assert runner.tick() == {"a": ["a-1"], "every": ["every-1"]}

    # Recién ejecutado: solo el de intervalo 0 toca
    assert {p.name for p in runner.due()} == {"every"}
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(29 * 60)
    assert {p.name for p in runner.due()} == {"every"}
    ctx.clock.advance(60)
    assert {p.name for p in runner.due()} == {"a", "every"}
    assert b.runs == 0 and unknown.runs == 0


def test_runner_isolates_errors(tmp_path: Path) -> None:
    cfg = make_config(
        bad=ProducerSettings(active=True, interval_minutes=10),
        good=ProducerSettings(active=True, interval_minutes=10),
    )
    ctx = make_ctx(tmp_path, config=cfg)
    bad, good = DummyProducer("bad", fail=True), DummyProducer("good")
    result = ProducerRunner(ctx, [bad, good]).tick()

    assert result == {"bad": [], "good": ["good-1"]}
    runs = {r["producer"]: r for r in producer_runs(ctx.db)}
    assert runs["bad"]["status"] == "error"
    assert "boom" in str(runs["bad"]["detail"])
    assert runs["good"]["status"] == "ok"
    assert runs["good"]["started_at"] == ctx.clock.now().isoformat()


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


def test_time_signal_text() -> None:
    assert time_signal_text(9) == "Son las nueve en punto en Radio Parra."


def test_time_signal_creates_stock_without_duplicates(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 23, 10, tzinfo=MADRID))
    producer = TimeSignalProducer()

    ids = producer.run(ctx)
    assert len(ids) == 2
    segs = {s["tags"][0]: s for s in ctx.db.list_segments(kind="time_signal")}
    assert set(segs) == {"hour:2026-09-25T00", "hour:2026-09-25T01"}
    midnight = segs["hour:2026-09-25T00"]
    assert midnight["status"] == "ready"
    assert midnight["script"] == "Son las doce de la noche en Radio Parra."
    assert midnight["title"] == "Señal horaria 00:00"
    assert midnight["voice_id"] == "host_main"
    assert midnight["producer"] == "time_signal"
    assert Path(midnight["audio_path"]).exists()
    assert segs["hour:2026-09-25T01"]["script"] == "Es la una en punto en Radio Parra."

    # Segunda ejecución inmediata: nada nuevo
    assert producer.run(ctx) == []
    assert len(ctx.db.list_segments(kind="time_signal")) == 2

    # Una hora después solo falta la siguiente
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(3600)
    assert len(producer.run(ctx)) == 1
    assert len(ctx.db.list_segments(kind="time_signal")) == 3


def test_time_signal_expires_past_hours(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 8, 30, tzinfo=MADRID))
    producer = TimeSignalProducer()
    producer.run(ctx)  # crea 09:00 y 10:00

    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(90 * 60)  # 10:00 → la de las 09 pasó hace 1h: aún no caduca
    producer.run(ctx)
    by_tag = {s["tags"][0]: s["status"] for s in ctx.db.list_segments(kind="time_signal")}
    assert by_tag["hour:2026-09-24T09"] == "ready"

    ctx.clock.advance(60)  # 10:01 → más de 1h
    producer.run(ctx)
    by_tag = {s["tags"][0]: s["status"] for s in ctx.db.list_segments(kind="time_signal")}
    assert by_tag["hour:2026-09-24T09"] == "done"
    assert by_tag["hour:2026-09-24T10"] == "ready"
    assert by_tag["hour:2026-09-24T11"] == "ready"
    # La caducada no se vuelve a crear
    assert len(by_tag) == 4


def test_time_signal_uses_station_timezone(tmp_path: Path) -> None:
    # 22:30 UTC = 00:30 en Madrid (verano)
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 22, 30, tzinfo=ZoneInfo("UTC")))
    TimeSignalProducer().run(ctx)
    tags = {s["tags"][0] for s in ctx.db.list_segments(kind="time_signal")}
    assert tags == {"hour:2026-09-25T01", "hour:2026-09-25T02"}


# ── Locutora ──────────────────────────────────────────────────────────────────

def test_host_intro_happy_path(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.db.add_segment(id="m1", kind="music", status="ready", title="Artista A — Tiny Desk", producer="music")
    ctx.db.log_play("m1", started_at="2026-09-24T14:00:00+02:00")

    ids = HostIntroProducer().run(ctx)
    assert len(ids) == 1
    seg = ctx.db.get_segment(ids[0])
    assert seg is not None
    assert seg["status"] == "ready"
    assert seg["script"] == GOOD_SCRIPT
    assert seg["voice_id"] == "host_main"
    assert seg["producer"] == "host_intro"
    assert seg["duration_s"] > 0
    assert seg["created_at"] == ctx.clock.now().isoformat()
    audio = Path(seg["audio_path"])
    assert audio.exists()
    assert audio.parent == tmp_path / "segments" / "host_intro"
    assert [p.name for p in audio.parent.iterdir()] == [audio.name]

    assert isinstance(ctx.llm, FakeLLM)
    call = ctx.llm.calls[0]
    assert call["json_schema"]["required"] == ["script"]
    assert call["temperature"] == 0.8
    assert call["max_tokens"] == 400
    assert "Radio Parra" in call["system"]
    assert "JSON" in call["system"]
    assert "16:20" in call["user"] and "tarde" in call["user"]
    assert "Artista A — Tiny Desk" in call["user"]


@pytest.mark.parametrize(
    "fixture",
    [
        "esto no es json",
        {"script": "   "},
        {"otro": "campo"},
        {"script": "Visita https://radioparra.example para más."},
        {"script": "Hola, soy una persona real y os hablo desde el estudio."},
    ],
)
def test_host_intro_rejects_invalid_output(tmp_path: Path, fixture: object) -> None:
    ctx = make_ctx(tmp_path, fixture=fixture)
    with pytest.raises(ValueError):
        HostIntroProducer().run(ctx)
    assert ctx.db.list_segments(kind="host_intro") == []


def test_host_intro_stock_cap(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = HostIntroProducer()
    for _ in range(3):
        assert len(producer.run(ctx)) == 1
    assert producer.run(ctx) == []
    assert isinstance(ctx.llm, FakeLLM)
    assert len(ctx.llm.calls) == 3


def test_parse_script_accepts_fenced_json_and_ai_disclosure() -> None:
    raw = '```json\n{"script": "No soy humana, soy la locutora IA de Radio Parra."}\n```'
    assert parse_script(raw) == "No soy humana, soy la locutora IA de Radio Parra."


def test_truncate_at_sentence() -> None:
    text = "Primera frase. " * 60
    out = truncate_at_sentence(text.strip(), 100)
    assert len(out) <= 100
    assert out.endswith(".")
    assert truncate_at_sentence("corto", 100) == "corto"
    long_word_run = "palabra " * 100
    cut = truncate_at_sentence(long_word_run.strip(), 50)
    assert len(cut) <= 51 and cut.endswith("…")
