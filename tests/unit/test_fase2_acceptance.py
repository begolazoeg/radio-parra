"""
Criterios de aceptación de la Fase 2 (§12 de ARCHITECTURE.md), sin red ni claves:

(a) "test que rechaza un dato ausente de las fuentes": el de §9 a nivel de grounding
    es ``tests/unit/test_grounding.py::test_grounding_rejects_fact_absent_from_sources``;
    aquí, el mismo criterio a nivel de productor (reintento → versión sin dato).
(b) "el 100 % de las intros con dato tienen claims trazables": muchas intros con un
    LLM que a veces responde mal; toda intro ``ready`` con datos tiene claims que
    citan fuentes guardadas y el grounding vuelve a pasar. ``radio audit host_intro``
    hace la misma comprobación sobre la BD.
(c) "``radio preview host_intro`` funciona con y sin fakes": con ``--fake`` de punta a
    punta, y sin ``--fake`` con los proveedores reales construidos desde la
    configuración pero con dobles en los bordes (cliente del SDK de Anthropic sobre
    un transporte simulado, binario ``piper`` falso, fuentes con
    ``httpx.MockTransport`` y un ``mpv`` falso).
(d) Decisión de la dueña: la descripción del episodio de NPR nunca llega al LLM.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
import yaml
from typer.testing import CliRunner

from radio.cli import app
from radio.core.models import Segment, SourceDoc
from radio.core.paths import db_path
from radio.core.store import DB
from radio.grounding import Claim, check_grounding, script_has_facts
from radio.music.feed import parse_entries
from radio.producers import MusicTinyDeskProducer, run_producer
from radio.producers import host_intro as host_intro_module
from radio.producers.host_intro import (
    HostIntroProducer,
    allowed_terms_for,
    audit_host_intros,
)
from radio.producers.host_intro_fake import fake_intro_responder, fake_sources
from radio.providers.llm.fake import FakeLLM
from tests.fixtures.host_intro import (
    FACT_FREE,
    GOOD,
    INVENTED,
    NOW,
    NPR_DESCRIPTION,
    NPR_ENCLOSURE,
    NPR_LINK,
    REPO,
    TITLE,
    SourcesMock,
    add_music,
    make_ctx,
)
from tests.fixtures.providers import install_fake_piper, install_piper_model
from tests.unit.test_llm_claude import make_message

FEED = REPO / "tests" / "fixtures" / "tinydesk_feed.xml"


def ready_intros(db: DB) -> list[Segment]:
    return db.list_segments(kind="host_intro", status="ready")


# ── (a) Un dato ausente de las fuentes se rechaza ────────────────────────────

def test_a_invented_fact_is_rejected_by_the_producer(tmp_path: Path) -> None:
    # El dato inventado ("dos premios Grammy") no está en ninguna fuente
    docs = [SourceDoc("wp:es:X", "Nube Ferrán ha publicado tres álbumes.", "u")]
    claims = [Claim(c["text"], c["source_id"]) for c in INVENTED["claims"]]
    assert not check_grounding(INVENTED["script"], claims, docs,
                               allowed_terms=("Nube Ferrán",)).ok

    llm = FakeLLM(script=[INVENTED, INVENTED, FACT_FREE])
    ctx = make_ctx(tmp_path, llm)
    add_music(ctx.db, tmp_path, "m1")
    run = run_producer(ctx, HostIntroProducer(ctx.config, client=SourcesMock().client(),
                                              sleep=lambda s: None))
    assert run.ok
    (intro,) = ready_intros(ctx.db)
    # Nunca sale al aire el guion con el dato inventado
    assert "Grammy" not in intro.meta["script"]
    attempts = intro.meta["attempts"]
    assert [a["variant"] for a in attempts] == ["grounded", "grounded", "fact_free"]
    assert [a["strict"] for a in attempts] == [False, True, True]
    assert all(any("Grammy" in p for p in a["problems"]) for a in attempts[:2])
    assert intro.meta["grounding"]["outcome"] == "fact_free"
    assert not intro.meta["grounding"]["has_facts"]


# ── (b) El 100 % de las intros con dato tienen claims trazables ──────────────

def _flaky_responder(seed: int) -> Any:
    """LLM que mezcla respuestas buenas y malas (reproducible)."""
    rng = random.Random(seed)

    def respond(system: str, user: str) -> Any:
        good = fake_intro_responder(system, user)
        kind = rng.choice([
            "good", "good", "good", "invented", "unclaimed", "wrong_source",
            "fact_free", "fake_fact_free", "malformed", "long",
        ])
        if kind == "good" or not good["claims"]:
            if kind == "fake_fact_free":
                return {"script": "Y ahora llega un grupo de Toronto al Tiny Desk. "
                                  "Un directo para escuchar con calma.", "claims": []}
            return good
        if kind == "invented":
            return {"script": good["script"].replace("2011", "1987"), "claims": good["claims"]}
        if kind == "unclaimed":
            return {"script": good["script"], "claims": []}
        if kind == "wrong_source":
            return {"script": good["script"],
                    "claims": [dict(c, source_id="wp:es:Otra") for c in good["claims"]]}
        if kind == "fact_free":
            return {"script": "Y ahora, desde el Tiny Desk, un concierto para escuchar "
                              "con calma. Aquí seguimos, sin prisas.", "claims": []}
        if kind == "fake_fact_free":
            return {"script": good["script"], "claims": []}
        if kind == "malformed":
            return '{"script": "sin cerrar'
        return {"script": good["script"] + " Y además." * 60, "claims": good["claims"]}

    return respond


def test_b_every_intro_with_facts_has_traceable_claims(tmp_path: Path) -> None:
    llm = FakeLLM(responder=_flaky_responder(seed=7))
    ctx = make_ctx(tmp_path, llm)
    for i in range(40):
        add_music(ctx.db, tmp_path, f"m{i:02d}", f"Grupo {i:02d}: Tiny Desk Concert",
                  minute=i)
    producer = HostIntroProducer(ctx.config, source_gatherer=fake_sources)
    producer.target_stock = 40
    ctx.config.producers.producers["host_intro"].target_stock = 40
    run = run_producer(ctx, producer)
    assert run.ok

    ready = ready_intros(ctx.db)
    with_facts = 0
    for intro in ready:
        meta = intro.meta
        allowed = allowed_terms_for(ctx.config.station.name, meta["artist"],
                                    meta["music_title"])
        assert meta["grounding"]["allowed_terms"] == allowed
        if not script_has_facts(meta["script"], allowed_terms=allowed):
            assert meta["grounding"]["outcome"] in ("grounded", "fact_free")
            continue
        with_facts += 1
        claims = [Claim.from_dict(c) for c in meta["claims"]]
        assert claims, intro.id
        ids = {s["id"] for s in meta["sources"]}
        assert all(c.source_id in ids for c in claims)
        assert all(s["url"] and s["license"] for s in meta["sources"])
        sources = [SourceDoc(s["id"], s["text"], s["url"]) for s in meta["sources"]]
        assert check_grounding(meta["script"], claims, sources, allowed_terms=allowed).ok
    # La mezcla ha ejercitado todos los caminos
    outcomes = [s.meta["grounding"]["outcome"]
                for s in ctx.db.list_segments(kind="host_intro")]
    assert with_facts >= 10
    assert outcomes.count("fact_free") >= 3 and outcomes.count("quarantined") >= 1
    assert len(ready) + outcomes.count("quarantined") == 40

    report = audit_host_intros(ctx.db, ctx.config.station.name)
    assert report.ok and report.checked == len(ready) and report.with_facts == with_facts


def test_b_audit_cli_passes_and_fails(tmp_path: Path) -> None:
    db_file = tmp_path / "state.db"
    db = DB(db_file)
    ctx = make_ctx(tmp_path, FakeLLM(GOOD), db=db)
    add_music(db, tmp_path, "m1")
    run_producer(ctx, HostIntroProducer(ctx.config, client=SourcesMock().client(),
                                        sleep=lambda s: None))
    (intro,) = ready_intros(db)
    db.close()
    args = ["audit", "host_intro", "--config-dir", str(REPO / "config"), "--db", str(db_file)]
    ok = CliRunner().invoke(app, args)
    assert ok.exit_code == 0, ok.output
    assert "(1 con datos)" in ok.output and "RESULTADO: OK" in ok.output

    # Una intro con datos y sin claims (p. ej. escrita a mano o por un fallo) no pasa
    with DB(db_file) as db:
        db.update_segment_meta(intro.id, dict(intro.meta, claims=[]))
    bad = CliRunner().invoke(app, args)
    assert bad.exit_code == 1
    assert "ningún claim" in bad.output and "RESULTADO: FALLO" in bad.output


# ── (c) radio preview host_intro con y sin fakes ─────────────────────────────

def test_c_preview_with_fakes(tmp_path: Path) -> None:
    out = tmp_path / "intro.wav"
    result = CliRunner().invoke(app, [
        "preview", "host_intro", "--fake", "--no-play", "--out", str(out),
        "--config-dir", str(REPO / "config"), "--prompts-dir", str(REPO / "prompts"),
    ])
    assert result.exit_code == 0, result.output
    assert "Resultado: grounded — estado ready (con datos)" in result.output
    assert "Claims (2):" in result.output and "Grounding: OK" in result.output
    assert "CC0" in result.output and "Coste: 0.0000 €" in result.output
    assert out.is_file() and out.stat().st_size > 44


def test_c_preview_rejects_unknown_producer_and_fake_register() -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["preview", "weather", "--fake"]).exit_code == 2
    assert runner.invoke(app, ["preview", "host_intro", "--fake", "--register"]).exit_code == 2


def _install_fake_mpv(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "mpv-args.json"
    exe = tmp_path / "bin" / "mpv"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"json.dump(sys.argv[1:], open({str(log)!r}, 'w'))\n", encoding="utf-8",
    )
    exe.chmod(0o755)
    return exe, log


@pytest.fixture
def real_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Config con Claude + Piper reales pero dobles en los bordes (sin red)."""
    piper = install_fake_piper(tmp_path)
    models = tmp_path / "models"
    install_piper_model(models, "es_ES-davefx-medium")
    mpv, mpv_log = _install_fake_mpv(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("grid.yaml", "voices.yaml", "producers.yaml"):
        (config_dir / name).write_text((REPO / "config" / name).read_text(encoding="utf-8"),
                                       encoding="utf-8")
    station = yaml.safe_load((REPO / "config" / "station.yaml").read_text(encoding="utf-8"))
    station["data_dir"] = str(data)
    station["providers"]["tts"]["extra"].update(
        binary=str(piper), models_dir=str(models), cache_dir=str(tmp_path / "tts-cache"),
    )
    station["audio"]["mpv_bin"] = str(mpv)
    (config_dir / "station.yaml").write_text(yaml.safe_dump(station, allow_unicode=True),
                                             encoding="utf-8")
    # Música en la BD real (como la deja music_tinydesk)
    with DB(db_path(data)) as db:
        add_music(db, data, "m1", link=NPR_LINK, enclosure_url=NPR_ENCLOSURE)

    # Claude: el cliente real del SDK sobre un transporte simulado
    requests: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        msg = make_message(json.dumps(GOOD, ensure_ascii=False), input_tokens=900,
                           output_tokens=120)
        return httpx2.Response(200, json=msg.model_dump(mode="json"))

    original = anthropic.Anthropic

    def fake_anthropic(**kwargs: Any) -> anthropic.Anthropic:
        return original(api_key="sk-ant-test-no-real-key", max_retries=0,
                        http_client=anthropic.DefaultHttpxClient(
                            transport=httpx2.MockTransport(handler)))

    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-no-real-key")
    sources = SourcesMock()
    monkeypatch.setattr(host_intro_module, "make_http_client", sources.client)
    return {"config_dir": config_dir, "data": data, "requests": requests,
            "sources": sources, "mpv_log": mpv_log}


def test_c_preview_without_fakes_uses_configured_providers(real_setup: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, [
        "preview", "host_intro", "--config-dir", str(real_setup["config_dir"]),
        "--prompts-dir", str(REPO / "prompts"),
    ])
    assert result.exit_code == 0, result.output
    assert "Resultado: grounded — estado ready (con datos)" in result.output
    assert "Modelo: claude-sonnet-5" in result.output
    # Claude: una sola llamada, Sonnet 5 sin temperature, con esquema JSON
    (req,) = real_setup["requests"]
    assert req["model"] == "claude-sonnet-5" and "temperature" not in req
    assert req["output_config"]["format"]["type"] == "json_schema"
    # Fuentes abiertas por el transporte simulado
    hosts = {r.url.host for r in real_setup["sources"].requests}
    assert "musicbrainz.org" in hosts
    # Se reprodujo con el mpv configurado
    played = json.loads(real_setup["mpv_log"].read_text(encoding="utf-8"))
    assert played[-1].endswith(".wav")
    # No registra nada, pero su coste cuenta para el presupuesto
    with DB(db_path(real_setup["data"])) as db:
        assert db.list_segments(kind="host_intro") == []
        (run,) = db.list_producer_runs()
        assert run.producer == "preview:host_intro" and run.ok
        assert run.cost_eur > 0 and run.tokens_in == 900 and run.tokens_out == 120
        assert run.tts_chars == len(GOOD["script"])
        assert db.month_cost_eur(NOW.replace(day=1, hour=0)) > 0
    # Sintetizó el binario de Piper configurado (el doble deja sus argumentos al lado)
    # y del audio no queda nada en data/tmp/
    left = list((real_setup["data"] / "tmp").iterdir())
    assert left and all(p.name.endswith(".wav.args.json") for p in left)
    piper = json.loads(left[0].read_text(encoding="utf-8"))
    assert piper["argv"][1].endswith("es_ES-davefx-medium.onnx")
    assert piper["text"] == GOOD["script"]


def test_c_preview_without_fakes_can_register(real_setup: dict[str, Any]) -> None:
    result = CliRunner().invoke(app, [
        "preview", "host_intro", "--register", "--no-play", "--music-id", "m1",
        "--config-dir", str(real_setup["config_dir"]), "--prompts-dir", str(REPO / "prompts"),
    ])
    assert result.exit_code == 0, result.output
    with DB(db_path(real_setup["data"])) as db:
        (intro,) = db.list_segments(kind="host_intro")
        assert intro.status == "ready" and intro.parent_id == "m1" and intro.path.is_file()
        assert audit_host_intros(db, "Radio Parra").ok


def test_c_preview_without_credentials_fails_cleanly(
    real_setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(real_setup["data"] / "sin-perfil"))
    result = CliRunner().invoke(app, [
        "preview", "host_intro", "--no-play",
        "--config-dir", str(real_setup["config_dir"]), "--prompts-dir", str(REPO / "prompts"),
    ])
    assert result.exit_code == 1
    assert "credenciales" in result.output
    assert real_setup["requests"] == []


# ── (d) La descripción de NPR nunca llega al LLM ─────────────────────────────

def test_d_npr_description_never_reaches_the_llm(tmp_path: Path) -> None:
    xml = FEED.read_bytes()
    descriptions = re.findall(r"<description>(.*?)</description>", xml.decode("utf-8"), re.S)
    descriptions = [d.strip() for d in descriptions if d.strip()]
    assert descriptions
    # music_tinydesk no guarda la descripción ni nada que no sea identificador/enlace
    drafts = [MusicTinyDeskProducer().draft_for(e) for e in parse_entries(xml)]
    for d in drafts:
        assert set(d.meta) == {"title", "guid", "published", "link", "artist", "tags",
                               "source", "enclosure_url"}

    llm = FakeLLM(script=[INVENTED, INVENTED, FACT_FREE])
    ctx = make_ctx(tmp_path, llm)
    # Música con todo lo que NPR da, y además una descripción "heredada" en meta
    add_music(ctx.db, tmp_path, "m0", TITLE, description=NPR_DESCRIPTION, summary=NPR_DESCRIPTION,
              link=NPR_LINK, enclosure_url=NPR_ENCLOSURE, guid="npr-guid-sentinel",
              published="2026-06-01T10:00:00+00:00")
    for n, d in enumerate(drafts, start=1):
        extra = {k: v for k, v in d.meta.items() if k != "title"}
        extra["description"] = descriptions[(n - 1) % len(descriptions)]   # meta heredada
        add_music(ctx.db, tmp_path, f"m{n}", d.meta["title"], minute=n, **extra)
    run = run_producer(ctx, HostIntroProducer(ctx.config, client=SourcesMock().client(),
                                              sleep=lambda s: None))
    assert run.ok and llm.calls

    forbidden = [NPR_DESCRIPTION, NPR_LINK, NPR_ENCLOSURE, "npr-guid-sentinel", "2026-06-01",
                 *descriptions, *(d.meta["link"] for d in drafts if d.meta["link"]),
                 *(d.meta["enclosure_url"] for d in drafts), *(d.meta["guid"] for d in drafts)]
    for call in llm.calls:
        prompt = call["system"] + "\n" + call["user"]
        for text in forbidden:
            assert text not in prompt, f"{text!r} ha llegado al LLM"
    # Lo único del episodio que llega es su título (como identificador)
    titles = [TITLE, *(d.meta["title"] for d in drafts)]
    for call in llm.calls:
        lines = [ln for ln in call["user"].splitlines() if "Título del episodio" in ln]
        assert len(lines) == 1 and any(ln.endswith(t) for ln in lines for t in titles)
