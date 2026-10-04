"""
Vinculación ``[host_intro, music]`` de punta a punta (§4.3 paso 6, §14 "ligada a la
música"): intros hechas por el productor real (con dobles de LLM, TTS y fuentes) y
emitidas por el motor de la emisora con reloj falso.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from radio.core.config import RadioConfig
from radio.producers import ProducerContext, run_producer
from radio.producers.host_intro import HostIntroProducer
from radio.producers.host_intro_fake import fake_intro_llm, fake_sources
from radio.providers.tts.fake import FakeTTS
from tests.unit.test_station_engine import REPO, T0, Rig, grid


def produce_intros(rig: Rig, tmp_path: Path) -> None:
    ctx = ProducerContext(
        db=rig.db, clock=rig.clock, llm=fake_intro_llm(), tts=FakeTTS(),
        config=RadioConfig.load(REPO / "config"), data_dir=tmp_path / "data",
        prompts_dir=REPO / "prompts",
    )
    run = run_producer(ctx, HostIntroProducer(ctx.config, source_gatherer=fake_sources))
    assert run.ok, run


def test_intro_airs_right_before_its_music_and_is_retired(tmp_path: Path) -> None:
    rig = Rig(tmp_path, grid_config=grid(["music"], signal=False))
    for i in range(6):
        rig.add(f"m{i}", duration=300.0, tags=[f"artist:a{i}"])
    produce_intros(rig, tmp_path)
    intros = {s.parent_id: s for s in rig.db.list_segments(kind="host_intro")}
    assert len(intros) == 6 and all(s.status == "ready" for s in intros.values())

    rig.engine.start()
    rig.run_until(T0 + timedelta(hours=1))
    log = rig.db.list_play_log()
    aired_intros = [(n, e) for n, e in enumerate(log) if e.kind == "host_intro"]
    assert aired_intros, "ninguna intro emitida"
    for n, entry in aired_intros:
        intro = rig.db.get_segment(entry.segment_id or "")
        assert intro is not None
        # play_log de ambos, la intro justo antes de su canción
        nxt = log[n + 1]
        assert nxt.kind == "music" and nxt.segment_id == intro.parent_id
        assert entry.ended_at is not None and nxt.started_at >= entry.ended_at
        if not entry.skipped:
            assert intro.status == "retired"        # la palabra emitida se retira
        assert rig.status(nxt.segment_id or "") == "ready"   # la música sigue en rotación
    # Nunca una intro suelta ni dos veces
    ids = [e.segment_id for _, e in aired_intros]
    assert len(ids) == len(set(ids))


def test_intro_of_non_ready_music_never_airs(tmp_path: Path) -> None:
    rig = Rig(tmp_path, grid_config=grid(["music"], signal=False))
    for i in range(3):
        rig.add(f"m{i}", duration=300.0, tags=[f"artist:a{i}"])
    produce_intros(rig, tmp_path)
    rig.db.update_segment_status("m0", "quarantined")
    rig.engine.start()
    rig.run_until(T0 + timedelta(hours=2))
    orphan = next(s for s in rig.db.list_segments(kind="host_intro") if s.parent_id == "m0")
    aired = {e.segment_id for e in rig.db.list_play_log()}
    assert orphan.id not in aired and "m0" not in aired


def test_intros_count_against_the_talk_budget(tmp_path: Path) -> None:
    # Canciones muy cortas: con una intro por canción se superaría el 22 %
    rig = Rig(tmp_path, grid_config=grid(["music"], signal=False))
    for i in range(12):
        rig.add(f"m{i:02d}", duration=25.0, tags=[f"artist:a{i}"])
    produce_intros(rig, tmp_path)
    rig.engine.start()
    end = T0 + timedelta(hours=1)
    rig.run_until(end)
    log = [e for e in rig.db.list_play_log() if e.ended_at is not None]
    talk = sum((e.ended_at - e.started_at).total_seconds()     # type: ignore[operator]
               for e in log if e.kind == "host_intro")
    total = sum((e.ended_at - e.started_at).total_seconds()    # type: ignore[operator]
                for e in log)
    assert 0 < talk <= 0.22 * max(total, 3600.0) + 1e-6
    # Alguna canción ha sonado sin su intro por el presupuesto
    music = [e for e in log if e.kind == "music"]
    assert len(music) > len([e for e in log if e.kind == "host_intro"])
