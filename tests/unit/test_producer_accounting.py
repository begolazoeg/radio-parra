"""
Contabilidad y ciclo de vida comunes a los productores (Fase 2):

- ``tts_chars`` solo cuenta lo sintetizado de verdad (no los aciertos de ``CachedTTS``);
- ``call_llm`` suma tokens y coste a ``producer_runs``, también de llamadas fallidas
  pero facturadas (``LLMError.cost_eur``);
- ``Draft.status = "quarantined"`` registra el segmento para revisión sin contarlo
  como stock creado;
- al expulsar una canción de la caché, sus intros vinculadas se retiran con ella.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.clock import FakeClock
from radio.core.config import RadioConfig
from radio.core.models import Segment, SegmentKind
from radio.core.store import DB
from radio.music.cache import evict_music_cache, retire_linked
from radio.producers import (
    Draft,
    ProducerContext,
    StagedProducer,
    call_llm,
    pick_voice,
    run_producer,
)
from radio.providers.errors import LLMRefusal
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.cache import CachedTTS
from radio.providers.tts.fake import FakeTTS

REPO = Path(__file__).parents[2]
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))


def make_ctx(tmp_path: Path, **kw: object) -> ProducerContext:
    data: dict[str, object] = {
        "db": DB(":memory:"), "clock": FakeClock(NOW), "llm": FakeLLM(), "tts": FakeTTS(),
        "config": RadioConfig.load(REPO / "config"), "data_dir": tmp_path,
    }
    data.update(kw)
    return ProducerContext(**data)  # type: ignore[arg-type]


class Echo(StagedProducer):
    """Registra los guiones dados; ``quarantine`` marca los que lo contienen."""
    name = "echo"
    kind: SegmentKind = "jingle"
    default_target_stock = 10

    def __init__(self, scripts: list[str]) -> None:
        super().__init__()
        self.scripts = scripts

    def gather(self, ctx: ProducerContext, wanted: int) -> list[Draft]:
        voice = pick_voice(ctx.config, "locutor_principal")
        return [Draft(voice=voice, script=s) for s in self.scripts[:wanted]]

    def write(self, ctx: ProducerContext, draft: Draft) -> Draft:
        if "cuarentena" in draft.script:
            draft.status = "quarantined"
        return draft


def test_tts_chars_only_count_cache_misses(tmp_path: Path) -> None:
    tts = CachedTTS(FakeTTS(), tmp_path / "cache")
    ctx = make_ctx(tmp_path, tts=tts)
    # Mismo texto dos veces: la segunda es un acierto de la caché y no se factura
    run = run_producer(ctx, Echo(["Hola", "Hola", "Adiós"]))
    assert run.ok and len(run.segment_ids) == 3
    assert (tts.hits, tts.misses) == (1, 2)
    stored = ctx.db.last_producer_run("echo")
    assert stored is not None and stored.tts_chars == len("Hola") + len("Adiós")


def test_quarantined_draft_is_registered_but_not_stock(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    run = run_producer(ctx, Echo(["Bien", "para cuarentena"]))
    assert run.ok and len(run.segment_ids) == 1 and run.quarantined == 1
    by_status = {s.status: s for s in ctx.db.list_segments(kind="jingle")}
    assert set(by_status) == {"ready", "quarantined"}
    # El de cuarentena conserva su audio para la revisión manual
    assert by_status["quarantined"].path.is_file()
    stored = ctx.db.last_producer_run("echo")
    assert stored is not None and stored.n_segments == 1
    assert ctx.db.stock_view(NOW).count("jingle") == 1


def test_call_llm_adds_cost_and_tokens_even_when_the_call_fails(tmp_path: Path) -> None:
    refusal = LLMRefusal("no", cost_eur=0.02, input_tokens=100, output_tokens=5)
    ctx = make_ctx(tmp_path, llm=FakeLLM(script=["{}", refusal], cost_eur=0.01))
    result = call_llm(ctx, "sistema breve", "usuario", temperature=0.2)
    assert result.cost_eur == pytest.approx(0.01)
    with pytest.raises(LLMRefusal):
        call_llm(ctx, "s", "u", temperature=0.2)
    assert ctx.stats.cost_eur == pytest.approx(0.03)
    assert ctx.stats.tokens_in == result.input_tokens + 100
    assert ctx.stats.tokens_out == result.output_tokens + 5


def test_fake_llm_script_repeats_last_answer() -> None:
    llm = FakeLLM(script=[{"a": 1}, "texto"])
    texts = [llm.complete("s", "u", temperature=0).text for _ in range(3)]
    assert texts == ['{"a": 1}', "texto", "texto"]


def _add(db: DB, tmp_path: Path, seg_id: str, kind: str, **kw: object) -> Segment:
    path = tmp_path / f"{seg_id}.wav"
    path.write_bytes(b"x" * 10)
    data: dict[str, object] = {
        "id": seg_id, "kind": kind, "factual": kind != "music", "path": path,
        "duration_s": 1.0, "created_at": NOW, "producer": "music_tinydesk",
    }
    data.update(kw)
    seg = Segment(**data)  # type: ignore[arg-type]
    db.add_segment(seg)
    return seg


def test_evicting_music_retires_its_linked_intros(tmp_path: Path) -> None:
    db = DB(":memory:")
    _add(db, tmp_path, "m1", "music", created_at=NOW.replace(minute=1))
    _add(db, tmp_path, "m2", "music", created_at=NOW.replace(minute=2))
    _add(db, tmp_path, "i1", "host_intro", parent_id="m1", producer="host_intro")
    _add(db, tmp_path, "i1q", "host_intro", parent_id="m1", producer="host_intro",
         status="quarantined")
    _add(db, tmp_path, "i2", "host_intro", parent_id="m2", producer="host_intro")
    report = evict_music_cache(db, producer="music_tinydesk", max_items=1)
    assert report.retired == ["m1"] and report.linked_retired == ["i1"]
    status = {s.id: s.status for s in db.list_segments()}
    assert status == {"m1": "retired", "m2": "ready", "i1": "retired",
                      "i1q": "quarantined", "i2": "ready"}
    assert not (tmp_path / "i1.wav").exists()
    assert (tmp_path / "i1q.wav").exists()      # la cuarentena se conserva para revisar
    assert retire_linked(db, "m2") == ["i2"]
