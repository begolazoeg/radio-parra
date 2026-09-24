"""
Tests del framework de productores (§4.2): pipeline por etapas, postproducción,
regla de gasto, registro, runner y señal horaria (sin red: FakeTTS / FakeClock).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import BudgetConfig, ProducersConfig, ProducerSettings, RadioConfig
from radio.core.models import AudioInfo, Segment, SegmentKind, StockView, Voice
from radio.core.store import DB
from radio.producers import (
    PRODUCERS,
    Draft,
    DraftRejected,
    FfmpegLoudnorm,
    MusicTinyDeskProducer,
    NullPost,
    ProducerContext,
    StagedProducer,
    TimeSignalProducer,
    build_producer,
    build_producers,
    choose_post,
    pick_voice,
    produce,
    run_producer,
    write_segment_audio,
)
from radio.producers.post import PostError
from radio.producers.time_signal import hour_phrase, time_signal_text
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.fake import FakeTTS

REPO = Path(__file__).parents[2]
MADRID = ZoneInfo("Europe/Madrid")
UTC = ZoneInfo("UTC")
NOW = datetime(2026, 9, 24, 16, 20, tzinfo=MADRID)


def make_config(budget: float | None = None, **producers: ProducerSettings) -> RadioConfig:
    base = RadioConfig.load(REPO / "config")
    update: dict[str, object] = {}
    if producers:
        update["producers"] = ProducersConfig(producers=producers)
    if budget is not None:
        update["station"] = base.station.model_copy(
            update={"budget": BudgetConfig(monthly_eur=budget)}
        )
    return base.model_copy(update=update) if update else base


def make_ctx(
    tmp_path: Path,
    *,
    now: datetime | None = None,
    config: RadioConfig | None = None,
) -> ProducerContext:
    return ProducerContext(
        db=DB(":memory:"),
        clock=FakeClock(now or NOW),
        llm=FakeLLM(),
        tts=FakeTTS(),
        config=config or make_config(),
        data_dir=tmp_path,
        prompts_dir=REPO / "prompts",
    )


def host(ctx: ProducerContext) -> Voice:
    return pick_voice(ctx.config, "locutor_principal")


def tmp_files(tmp_path: Path) -> list[Path]:
    tmp = tmp_path / "tmp"
    return sorted(tmp.iterdir()) if tmp.exists() else []


# ── Productores de prueba ─────────────────────────────────────────────────────

class ScriptedProducer(StagedProducer):
    """Pipeline completo con TTS falso; guiones fijos y etapas espiables."""
    name = "scripted"
    kind: SegmentKind = "jingle"
    factual = False
    default_target_stock = 2

    def __init__(self, scripts: Sequence[str] = ("Uno", "Dos"), **kw: object) -> None:
        super().__init__()
        self.scripts = list(scripts)
        self.calls: list[str] = []
        self.fail_tts_on: str | None = kw.get("fail_tts_on")  # type: ignore[assignment]

    def gather(self, ctx: ProducerContext, wanted: int) -> list[Draft]:
        self.calls.append(f"gather:{wanted}")
        return [Draft(voice=host(ctx), meta={"title": s, "raw": s}) for s in self.scripts[:wanted]]

    def write(self, ctx: ProducerContext, draft: Draft) -> Draft:
        self.calls.append("write")
        draft.script = str(draft.meta["raw"])
        return draft

    def validate(self, ctx: ProducerContext, draft: Draft) -> list[str]:
        self.calls.append("validate")
        problems = super().validate(ctx, draft)
        if "prohibido" in draft.script:
            problems.append("tema prohibido")
        return problems

    def tts(self, ctx: ProducerContext, draft: Draft) -> Draft:
        self.calls.append("tts")
        draft = super().tts(ctx, draft)
        if draft.script == self.fail_tts_on:
            raise OSError("disco")
        return draft


class DummyProducer:
    """Productor mínimo (protocolo, sin plantilla) para probar el runner."""
    kind: SegmentKind = "jingle"
    factual = False
    billable = True

    def __init__(self, name: str, fail: bool = False, deficit: int = 0) -> None:
        self.name = name
        self.fail = fail
        self.target_stock = deficit
        self._deficit = deficit
        self.runs = 0

    def deficit(self, stock: StockView, now: datetime) -> int:
        return self._deficit

    def produce(self, ctx: ProducerContext) -> list[Segment]:
        self.runs += 1
        ctx.stats.tts_chars += 7
        ctx.stats.cost_eur += 0.5
        if self.fail:
            raise RuntimeError("boom")
        return [Segment(
            id=f"{self.name}-{self.runs}", kind=self.kind, factual=False,
            path=Path("/x.wav"), duration_s=1.0, created_at=ctx.clock.now(), producer=self.name,
        )]


class RecordingPost:
    """Post que escribe un archivo nuevo junto al original."""

    def __init__(self) -> None:
        self.seen: list[Path] = []

    def process(self, audio: AudioInfo) -> AudioInfo:
        self.seen.append(audio.path)
        out = audio.path.with_name(audio.path.stem + ".post.wav")
        out.write_bytes(audio.path.read_bytes())
        return AudioInfo(path=out, duration_s=audio.duration_s + 1)


# ── Helpers de base ───────────────────────────────────────────────────────────

def test_write_segment_audio_is_atomic(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    info = write_segment_audio(ctx, kind="time_signal", seg_id="X1", text="Hola", voice=host(ctx))
    assert info.path == tmp_path / "stock" / "time_signal" / "X1.wav"
    assert info.path.exists() and info.duration_s > 0
    assert tmp_files(tmp_path) == []


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
    assert tmp_files(tmp_path) == []
    assert not (tmp_path / "stock" / "jingle").exists()


def test_pick_voice() -> None:
    cfg = make_config()
    assert pick_voice(cfg, "locutor_principal").consent
    with pytest.raises(ValueError):
        pick_voice(cfg, "nope")


# ── Registro ──────────────────────────────────────────────────────────────────

def test_registry_builds_configured_producers() -> None:
    assert set(PRODUCERS) >= {"time_signal", "music_tinydesk"}
    cfg = make_config(
        time_signal=ProducerSettings(active=True, target_stock=3),
        music_tinydesk=ProducerSettings(active=False, target_stock=12),
        weather=ProducerSettings(active=True),          # sin implementar: se ignora
    )
    ts = build_producer("time_signal", cfg)
    assert isinstance(ts, TimeSignalProducer) and ts.kind == "time_signal"
    assert ts.target_stock == 3
    music = build_producer("music_tinydesk", cfg)
    assert isinstance(music, MusicTinyDeskProducer)
    assert (music.kind, music.target_stock, music.factual) == ("music", 12, False)
    assert [p.name for p in build_producers(cfg)] == ["time_signal"]
    assert {p.name for p in build_producers(cfg, only_active=False)} == {
        "time_signal", "music_tinydesk"
    }
    with pytest.raises(KeyError, match="disponibles"):
        build_producer("weather", cfg)


# ── Pipeline por etapas ───────────────────────────────────────────────────────

def test_pipeline_runs_stages_and_registers_atomically(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = ScriptedProducer()
    created = producer.produce(ctx)

    assert producer.calls == ["gather:2"] + ["write", "validate", "tts"] * 2
    assert [s.meta["script"] for s in created] == ["Uno", "Dos"]
    for seg in created:
        stored = ctx.db.get_segment(seg.id)
        assert stored is not None and stored.status == "ready"
        assert stored.path == tmp_path / "stock" / "jingle" / f"{seg.id}.wav"
        assert stored.path.is_file()
        assert stored.voice_id == "locutor_principal"
        assert stored.producer == "scripted"
    assert tmp_files(tmp_path) == []
    assert ctx.stats.n_segments == 2
    assert ctx.stats.tts_chars == len("Uno") + len("Dos")
    # Stock lleno: déficit 0, no se produce nada más
    assert producer.produce(ctx) == []


def test_pipeline_validate_rejects_without_failing(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = ScriptedProducer(["esto está prohibido", "  ", "Bien"])
    producer.target_stock = producer.default_target_stock = 3
    created = producer.produce(ctx)
    assert [s.meta["script"] for s in created] == ["Bien"]
    assert ctx.stats.rejected == 2
    assert producer.calls.count("tts") == 1           # los rechazados no llegan a TTS
    assert tmp_files(tmp_path) == []


def test_pipeline_stage_failure_leaves_no_tmp_and_no_row(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = ScriptedProducer(["Uno", "Dos"], fail_tts_on="Dos")
    with pytest.raises(OSError):
        producer.produce(ctx)
    segs = ctx.db.list_segments(kind="jingle")
    assert [s.meta["script"] for s in segs] == ["Uno"]  # lo ya registrado se queda
    assert tmp_files(tmp_path) == []
    assert len(list((tmp_path / "stock" / "jingle").iterdir())) == 1


def test_pipeline_post_stage_replaces_tmp_audio(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    post = RecordingPost()
    ctx.post = post
    created = ScriptedProducer(["Uno"]).produce(ctx)
    assert len(post.seen) == 1 and post.seen[0].parent == tmp_path / "tmp"
    seg = created[0]
    assert seg.path.suffix == ".wav" and seg.path.is_file()
    assert seg.duration_s == pytest.approx(len("Uno") / 15.0 + 1)
    assert tmp_files(tmp_path) == []


def test_register_failure_removes_moved_file(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = ScriptedProducer(["Uno"])
    draft = producer.write(ctx, producer.gather(ctx, 1)[0])
    draft = producer.tts(ctx, draft)
    # Una fila con el mismo id hace fallar la inserción
    ctx.db.add_segment(Segment(
        id=draft.id, kind="jingle", factual=False, path=Path("/otro.wav"),
        duration_s=1, created_at=NOW, producer="x",
    ))
    with pytest.raises(Exception, match="UNIQUE"):
        producer.register(ctx, draft)
    assert not (tmp_path / "stock" / "jingle" / f"{draft.id}.wav").exists()


def test_register_applies_state_delta_in_same_transaction(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = ScriptedProducer(["Uno"])
    draft = producer.tts(ctx, producer.write(ctx, producer.gather(ctx, 1)[0]))
    draft.universe, draft.state_delta = "liga", {"jornada": 1}
    producer.register(ctx, draft)
    assert ctx.db.get_universe_state("liga") == (1, {"jornada": 1})

    # Si el estado falla (versión obsoleta), tampoco queda el segmento
    draft2 = producer.tts(ctx, producer.write(ctx, producer.gather(ctx, 1)[0]))
    draft2.universe, draft2.state_delta = "liga", {"jornada": 2}

    def stale(*_a: object, **_k: object) -> None:
        raise RuntimeError("versión obsoleta")

    producer.apply_state_delta = stale  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        producer.register(ctx, draft2)
    assert ctx.db.get_segment(draft2.id) is None
    assert not (tmp_path / "stock" / "jingle" / f"{draft2.id}.wav").exists()


def test_register_rejects_zero_duration(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    producer = ScriptedProducer(["Uno"])
    draft = producer.tts(ctx, producer.write(ctx, producer.gather(ctx, 1)[0]))
    assert draft.audio is not None
    draft.audio = AudioInfo(path=draft.audio.path, duration_s=0.0)
    with pytest.raises(DraftRejected):
        producer.register(ctx, draft)


# ── Postproducción ────────────────────────────────────────────────────────────

LOUDNORM_STDERR = """
[Parsed_loudnorm_0 @ 0x55]
{
	"input_i" : "-23.54",
	"input_tp" : "-7.12",
	"input_lra" : "5.60",
	"input_thresh" : "-34.10",
	"output_i" : "-16.02",
	"output_tp" : "-1.50",
	"output_lra" : "4.90",
	"output_thresh" : "-26.50",
	"normalization_type" : "dynamic",
	"target_offset" : "0.02"
}
"""


def test_ffmpeg_loudnorm_two_pass_with_fake_runner(tmp_path: Path) -> None:
    src = tmp_path / "a.wav"
    FakeTTS().synthesize("Hola, radio", Voice("v", "host", "fake", "v", True, "ok"), src)
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> tuple[int, str]:
        calls.append(list(args))
        if "null" in args:
            return 0, LOUDNORM_STDERR
        Path(args[-1]).write_bytes(src.read_bytes())    # "ffmpeg" copia el audio
        return 0, ""

    post = FfmpegLoudnorm(-16.0, runner=runner)
    out = post.process(AudioInfo(path=src, duration_s=1.0))

    measure, apply = calls
    assert measure[0] == "ffmpeg" and measure[-3:] == ["-f", "null", "-"]
    assert "loudnorm=I=-16.0:TP=-1.5:LRA=11.0:print_format=json" in measure[measure.index("-af") + 1]
    af = apply[apply.index("-af") + 1]
    assert "silenceremove" in af and "areverse" in af
    assert "measured_I=-23.54" in af and "offset=0.02" in af and "linear=true" in af
    assert apply[-3:] == ["-c:a", "pcm_s16le", str(tmp_path / "a.post.wav")]
    assert out.path == tmp_path / "a.post.wav"
    assert out.duration_s == pytest.approx(len("Hola, radio") / 15.0, abs=0.01)


def test_ffmpeg_loudnorm_errors(tmp_path: Path) -> None:
    src = tmp_path / "a.wav"
    src.write_bytes(b"x")
    with pytest.raises(PostError, match="medida"):
        FfmpegLoudnorm(runner=lambda a: (1, "fallo")).process(AudioInfo(src, 1.0))
    with pytest.raises(PostError, match="loudnorm"):
        FfmpegLoudnorm(runner=lambda a: (0, "sin json")).process(AudioInfo(src, 1.0))

    def bad_output(args: Sequence[str]) -> tuple[int, str]:
        if "null" in args:
            return 0, LOUDNORM_STDERR
        Path(args[-1]).write_bytes(b"no es audio")
        return 0, ""

    with pytest.raises(PostError, match="vacío"):
        FfmpegLoudnorm(runner=bad_output).process(AudioInfo(src, 1.0))
    assert not (tmp_path / "a.post.wav").exists()


def test_choose_post(caplog: pytest.LogCaptureFixture) -> None:
    cfg = make_config()
    with caplog.at_level(logging.WARNING):
        assert isinstance(choose_post(cfg, which=lambda _c: None), NullPost)
    assert "ffmpeg" in caplog.text
    post = choose_post(cfg, which=lambda _c: "/usr/bin/ffmpeg")
    assert isinstance(post, FfmpegLoudnorm)
    assert post.target_lufs == cfg.station.loudness_lufs and post.ffmpeg == "/usr/bin/ffmpeg"
    parsed = FfmpegLoudnorm.parse_measurement(LOUDNORM_STDERR)
    assert json.dumps(parsed)  # serializable
    assert parsed["input_i"] == "-23.54"


# ── Regla de gasto ────────────────────────────────────────────────────────────

def _spend(ctx: ProducerContext, eur: float, at: datetime) -> None:
    run = ctx.db.start_producer_run("otro", at)
    ctx.db.finish_producer_run(run, ended_at=at, ok=True, cost_eur=eur)


def test_budget_exhausted_skips_and_records(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, config=make_config(budget=1.0))
    _spend(ctx, 0.6, datetime(2026, 8, 31, 23, 0, tzinfo=MADRID))   # mes anterior: no cuenta
    _spend(ctx, 0.6, datetime(2026, 9, 1, 0, 30, tzinfo=MADRID))
    producer = DummyProducer("caro", deficit=1)
    assert run_producer(ctx, producer).ok is True               # 0.6 < 1.0
    result = run_producer(ctx, producer)                         # 0.6 + 0.5 >= 1.0
    assert result.ok is False and result.error == "presupuesto agotado"
    assert producer.runs == 1
    last = ctx.db.last_producer_run("caro")
    assert last is not None and last.ok is False and last.error == "presupuesto agotado"


def test_budget_does_not_apply_to_free_producers(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, config=make_config(budget=0.0))
    producer = DummyProducer("gratis", deficit=1)
    producer.billable = False
    assert run_producer(ctx, producer).ok is True


# ── Runner (jobs) ─────────────────────────────────────────────────────────────

def test_run_producer_records_usage_and_isolates_errors(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    good = run_producer(ctx, DummyProducer("good"))
    bad = run_producer(ctx, DummyProducer("bad", fail=True))
    assert good.ok and good.segment_ids == ("good-1",)
    assert not bad.ok and bad.error == "boom"
    runs = {r.producer: r for r in ctx.db.list_producer_runs()}
    assert runs["good"].n_segments == 1 and runs["good"].tts_chars == 7
    assert runs["good"].cost_eur == pytest.approx(0.5)
    assert runs["bad"].ok is False and runs["bad"].cost_eur == pytest.approx(0.5)
    assert runs["good"].started_at == ctx.clock.now() and runs["good"].ended_at is not None


def test_produce_all_selects_by_active_cron_and_deficit(tmp_path: Path) -> None:
    cfg = make_config(
        cron_due=ProducerSettings(active=True, cron="*/30 * * * *"),
        needs=ProducerSettings(active=True),
        full=ProducerSettings(active=True),
        off=ProducerSettings(active=False, cron="* * * * *"),
        weather=ProducerSettings(active=True),          # no está en el registro
    )
    ctx = make_ctx(tmp_path, config=cfg)
    instances = {
        "cron_due": DummyProducer("cron_due"),
        "needs": DummyProducer("needs", deficit=2),
        "full": DummyProducer("full"),
        "off": DummyProducer("off", deficit=5),
    }
    report = produce(ctx, producers=instances)
    assert {r.name: r.reason for r in report.results} == {
        "cron_due": "cron", "needs": "déficit 2",
    }
    assert report.skipped == {
        "full": "sin déficit ni cron pendiente", "off": "inactivo", "weather": "no implementado",
    }
    assert report.ok
    # Recién ejecutado: el cron ya no está pendiente
    again = produce(ctx, producers=instances)
    assert [r.name for r in again.results] == ["needs"]


def test_produce_explicit_runs_inactive_and_reports_unknown(tmp_path: Path) -> None:
    cfg = make_config(off=ProducerSettings(active=False))
    ctx = make_ctx(tmp_path, config=cfg)
    off = DummyProducer("off")
    report = produce(ctx, ["off", "nope"], producers={"off": off})
    assert off.runs == 1
    by_name = {r.name: r for r in report.results}
    assert by_name["off"].ok and by_name["off"].reason == "explícito"
    assert not by_name["nope"].ok and "desconocido" in str(by_name["nope"].error)
    assert not report.ok


def test_produce_isolates_failures_and_keeps_existing(tmp_path: Path) -> None:
    cfg = make_config(
        bad=ProducerSettings(active=True, cron="* * * * *"),
        good=ProducerSettings(active=True, cron="* * * * *"),
    )
    ctx = make_ctx(tmp_path, config=cfg)
    existing = Segment(
        id="keep", kind="jingle", factual=False, path=tmp_path / "keep.wav",
        duration_s=3, created_at=NOW, producer="bad",
    )
    ctx.db.add_segment(existing)
    report = produce(ctx, producers={
        "bad": DummyProducer("bad", fail=True), "good": DummyProducer("good"),
    })
    assert [(r.name, r.ok) for r in report.results] == [("bad", False), ("good", True)]
    assert ctx.db.get_segment("keep") == existing
    assert "ERROR: boom" in report.to_text()


def test_produce_dry_run_records_nothing(tmp_path: Path) -> None:
    cfg = make_config(a=ProducerSettings(active=True, cron="* * * * *"))
    ctx = make_ctx(tmp_path, config=cfg)
    a = DummyProducer("a")
    report = produce(ctx, dry_run=True, producers={"a": a})
    assert [(r.name, r.dry_run) for r in report.results] == [("a", True)]
    assert a.runs == 0 and ctx.db.list_producer_runs() == []
    assert "tocaría" in report.to_text()


def test_produce_with_registry_time_signal(tmp_path: Path) -> None:
    cfg = make_config(time_signal=ProducerSettings(active=True, target_stock=2))
    ctx = make_ctx(tmp_path, config=cfg)
    report = produce(ctx)                   # sin cron: toca por déficit
    assert [(r.name, r.reason, len(r.segment_ids)) for r in report.results] == [
        ("time_signal", "déficit 2", 2)
    ]
    run = ctx.db.last_producer_run("time_signal")
    assert run is not None and run.n_segments == 2 and run.tts_chars > 0
    assert produce(ctx).results == []


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


def test_time_signal_deficit_counts_missing_hours(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 23, 10, tzinfo=MADRID))
    producer = TimeSignalProducer(ctx.config)
    now = ctx.clock.now()
    assert producer.deficit(StockView(), now) == 2
    producer.produce(ctx)
    assert producer.deficit(ctx.db.stock_view(now), now) == 0
    later = now + timedelta(hours=1)
    assert producer.deficit(ctx.db.stock_view(later), later) == 1


def test_time_signal_creates_stock_without_duplicates(tmp_path: Path) -> None:
    now = datetime(2026, 9, 24, 23, 10, tzinfo=MADRID)
    ctx = make_ctx(tmp_path, now=now)
    producer = TimeSignalProducer()

    created = producer.produce(ctx)
    assert len(created) == 2
    segs = {s.tags[0]: s for s in ctx.db.list_segments(kind="time_signal")}
    assert set(segs) == {"hour:2026-09-25T00", "hour:2026-09-25T01"}
    midnight = segs["hour:2026-09-25T00"]
    assert midnight.status == "ready"
    assert midnight.factual is True
    assert midnight.priority == 1
    assert midnight.meta["script"] == "Son las doce de la noche en Radio Parra."
    assert midnight.meta["sources"][0]["id"] == "reloj"
    assert "hour" not in midnight.meta
    assert midnight.title == "Señal horaria 00:00"
    assert midnight.voice_id == "locutor_principal"
    assert midnight.producer == "time_signal"
    assert midnight.created_at == now
    assert midnight.expires_at == datetime(2026, 9, 25, 0, 5, tzinfo=MADRID)
    assert midnight.path.exists()
    assert midnight.path.parent == tmp_path / "stock" / "time_signal"
    assert segs["hour:2026-09-25T01"].meta["script"] == "Es la una en punto en Radio Parra."
    assert tmp_files(tmp_path) == []

    assert producer.produce(ctx) == []
    assert len(ctx.db.list_segments(kind="time_signal")) == 2

    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(3600)
    assert len(producer.produce(ctx)) == 1
    assert len(ctx.db.list_segments(kind="time_signal")) == 3


def test_time_signal_hours_ahead_follow_target_stock(tmp_path: Path) -> None:
    cfg = make_config(time_signal=ProducerSettings(active=True, target_stock=4, cron="0 * * * *"))
    ctx = make_ctx(tmp_path, config=cfg)
    assert len(TimeSignalProducer().produce(ctx)) == 4


def test_time_signal_expires_past_hours(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 8, 30, tzinfo=MADRID))
    producer = TimeSignalProducer()
    producer.produce(ctx)  # crea 09:00 y 10:00

    def statuses() -> dict[str, str]:
        return {s.tags[0]: s.status for s in ctx.db.list_segments(kind="time_signal")}

    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(34 * 60)  # 09:04 → la de las 09 sigue en ventana
    producer.produce(ctx)
    assert statuses()["hour:2026-09-24T09"] == "ready"
    assert ctx.db.stock_view(ctx.clock.now()).count("time_signal") == 3

    ctx.clock.advance(60)  # 09:05 → fuera de ventana: caduca
    producer.produce(ctx)
    by_tag = statuses()
    assert by_tag["hour:2026-09-24T09"] == "expired"
    assert by_tag["hour:2026-09-24T10"] == "ready"
    assert by_tag["hour:2026-09-24T11"] == "ready"
    assert len(by_tag) == 3


def test_time_signal_regenerates_quarantined(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 8, 30, tzinfo=MADRID))
    producer = TimeSignalProducer()
    first = producer.produce(ctx)
    ctx.db.update_segment_status(first[0].id, "quarantined")
    again = producer.produce(ctx)
    assert len(again) == 1
    assert again[0].tags == ("hour:2026-09-24T09",)


def test_time_signal_uses_station_timezone(tmp_path: Path) -> None:
    # 22:30 UTC = 00:30 en Madrid (verano)
    ctx = make_ctx(tmp_path, now=datetime(2026, 9, 24, 22, 30, tzinfo=UTC))
    TimeSignalProducer().produce(ctx)
    tags = {s.tags[0] for s in ctx.db.list_segments(kind="time_signal")}
    assert tags == {"hour:2026-09-25T01", "hour:2026-09-25T02"}
    seg = ctx.db.find_by_meta("time_signal", "tags", "hour:2026-09-25T01")
    assert seg is not None and seg.expires_at is not None
    assert seg.expires_at - timedelta(minutes=5) == datetime(2026, 9, 25, 1, tzinfo=MADRID)
