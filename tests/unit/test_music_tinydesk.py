"""
Tests del productor ``music_tinydesk`` y de ``radio.music.feed`` / ``radio.music.cache``.

Sin red: el feed es ``tests/fixtures/tinydesk_feed.xml`` (hosts ficticios ``.invalid``)
servido con ``httpx.MockTransport``, y los audios son WAV generados con ``wave``.
Ninguna URL real aparece aquí: la del feed es la decisión abierta #8.
"""

from __future__ import annotations

import io
import shutil
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from typer.testing import CliRunner

from radio.cli import app
from radio.core.clock import FakeClock
from radio.core.config import ProducersConfig, ProducerSettings, RadioConfig
from radio.core.models import Segment
from radio.core.store import DB
from radio.music.cache import evict_music_cache
from radio.music.feed import (
    USER_AGENT,
    FeedEntry,
    artist_from_title,
    clean_text,
    parse_entries,
)
from radio.producers import (
    MusicTinyDeskProducer,
    ProducerContext,
    ProducerError,
    produce,
    run_producer,
)
from radio.producers.music_tinydesk import MISSING_FEED_URL
from radio.providers.llm.fake import FakeLLM
from radio.providers.tts.fake import FakeTTS

REPO = Path(__file__).parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "tinydesk_feed.xml"
MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=MADRID)
FEED_URL = "https://feeds.fixture.invalid/tinydesk.xml"
MEDIA = "https://media.fixture.invalid"


def wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeServer:
    """Servidor falso: feed con ETag + audios. Registra cada petición."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.etag = '"v1"'
        self.fail: type[Exception] | None = None
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail("sin red", request=request)  # type: ignore[call-arg]
        url = str(request.url)
        if url in self.overrides:
            return self.overrides[url](request)
        if url == FEED_URL:
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(
                200, content=FIXTURE.read_bytes(),
                headers={"Content-Type": "application/rss+xml", "ETag": self.etag},
            )
        if url == f"{MEDIA}/audio/roto.wav":
            return httpx.Response(200, content=b"<html>no</html>",
                                  headers={"Content-Type": "text/html; charset=utf-8"})
        if url.startswith(f"{MEDIA}/audio/"):
            return httpx.Response(200, content=wav_bytes(),
                                  headers={"Content-Type": "audio/wav"})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def make_config(**params: object) -> RadioConfig:
    base = RadioConfig.load(REPO / "config")
    merged: dict[str, object] = {"feed_url": FEED_URL, "download_delay_s": 0.5}
    merged.update(params)
    target = int(merged.pop("target_stock", 2))  # type: ignore[call-overload]
    settings = ProducerSettings(active=True, target_stock=target, params=merged)
    return base.model_copy(
        update={"producers": ProducersConfig(producers={"music_tinydesk": settings})}
    )


def make_ctx(tmp_path: Path, config: RadioConfig) -> ProducerContext:
    return ProducerContext(
        db=DB(":memory:"), clock=FakeClock(NOW), llm=FakeLLM(), tts=FakeTTS(),
        config=config, data_dir=tmp_path,
    )


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def make_producer(
    server: FakeServer,
    sleeps: list[float] | None = None,
    config: RadioConfig | None = None,
) -> MusicTinyDeskProducer:
    return MusicTinyDeskProducer(
        config,
        client=server.client(),
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


def tmp_leftovers(tmp_path: Path) -> list[Path]:
    tmp = tmp_path / "tmp"
    return list(tmp.iterdir()) if tmp.exists() else []


# ── Feed ──────────────────────────────────────────────────────────────────────

def test_parse_entries_audio_only_newest_first() -> None:
    entries = parse_entries(FIXTURE.read_bytes())
    assert [e.guid for e in entries] == [
        "fixture-guid-005", "fixture-guid-html", "fixture-guid-004",
        "fixture-guid-003", "fixture-guid-001",
    ]
    rosalia = entries[2]
    assert rosalia.title == "Rosalía: Tiny Desk (Home) Concert"
    assert rosalia.mime == "audio/x-wav" and rosalia.ext == ".wav"
    assert rosalia.description == (
        "Rosalía toca tres canciones desde casa & con amigos. Setlist: Uno, Dos."
    )
    assert rosalia.link == "https://podcast.fixture.invalid/episodes/rosalia"
    assert rosalia.published == datetime(2026, 9, 10, 14, 0, tzinfo=ZoneInfo("UTC"))
    assert entries[0].ext == ".wav"          # la query string no confunde la extensión


def test_parse_entries_rejects_garbage() -> None:
    from radio.music.feed import FeedError

    with pytest.raises(FeedError):
        parse_entries(b"\x00\x01 esto no es xml")


@pytest.mark.parametrize(
    ("title", "artist"),
    [
        ("Rosalía: Tiny Desk (Home) Concert", "Rosalía"),
        ("Anderson .Paak & The Free Nationals: Tiny Desk Concert",
         "Anderson .Paak & The Free Nationals"),
        ("Mon Laferte - Tiny Desk Concert", "Mon Laferte"),
        ("Sessions: Mon Laferte", None),
        ("Tiny Desk Concert", None),
    ],
)
def test_artist_from_title(title: str, artist: str | None) -> None:
    assert artist_from_title(title) == artist


def test_clean_text_and_ext_fallback() -> None:
    assert clean_text("<p>Hola&nbsp;<b>mundo</b></p>\n\n<p>adiós</p>") == "Hola mundo adiós"
    assert clean_text("x" * 50, limit=10) == "x" * 10
    entry = FeedEntry("g", "t", f"{MEDIA}/a", "audio/mpeg", None, None, "", "")
    assert entry.ext == ".mp3"


# ── Productor ─────────────────────────────────────────────────────────────────

def test_happy_path_downloads_newest_audio(tmp_path: Path, server: FakeServer) -> None:
    ctx = make_ctx(tmp_path, make_config(target_stock=2))
    sleeps: list[float] = []
    created = make_producer(server, sleeps).produce(ctx)

    # Déficit 2: los dos más recientes con audio válido ("roto" se descarta)
    assert [s.meta["guid"] for s in created] == ["fixture-guid-005", "fixture-guid-004"]
    assert ctx.stats.rejected == 1
    assert server.urls() == [
        FEED_URL,
        f"{MEDIA}/audio/anderson-paak.wav?source=rss",
        f"{MEDIA}/audio/roto.wav",
        f"{MEDIA}/audio/rosalia",
    ]
    assert all(r.headers["User-Agent"] == USER_AGENT for r in server.requests)
    assert sleeps == [0.5, 0.5]                     # pausa entre descargas secuenciales
    assert f"{MEDIA}/video/only.mp4" not in server.urls()

    paak = ctx.db.find_by_meta("music", "guid", "fixture-guid-005")
    assert paak is not None
    assert paak.status == "ready" and paak.factual is False and paak.kind == "music"
    assert paak.producer == "music_tinydesk"
    assert paak.path == tmp_path / "stock" / "music" / f"{paak.id}.wav"
    assert paak.path.is_file()
    assert paak.duration_s == pytest.approx(1.0, abs=0.01)
    assert paak.tags == ("source:tiny_desk", "artist:anderson-paak-the-free-nationals")
    assert paak.meta["artist"] == "Anderson .Paak & The Free Nationals"
    assert paak.meta["link"] == "https://podcast.fixture.invalid/episodes/anderson-paak"
    assert paak.meta["description"] == "Funk en la oficina."
    assert paak.meta["published"] == "2026-09-21T12:00:00+00:00"
    assert paak.meta["source"] == "rss"
    assert "sources" not in paak.meta
    rosalia = ctx.db.find_by_meta("music", "guid", "fixture-guid-004")
    assert rosalia is not None and rosalia.tags == ("source:tiny_desk", "artist:rosalia")
    assert tmp_leftovers(tmp_path) == []


def test_rerun_dedups_by_guid_and_uses_conditional_get(tmp_path: Path, server: FakeServer) -> None:
    ctx = make_ctx(tmp_path, make_config(target_stock=2))
    producer = make_producer(server)
    producer.produce(ctx)
    server.requests.clear()

    # Stock lleno (déficit 0): rota 1 episodio nuevo, sin repetir ni reintentar "roto"
    created = producer.produce(ctx)
    assert [s.meta["guid"] for s in created] == ["fixture-guid-003"]
    assert server.requests[0].headers["If-None-Match"] == '"v1"'   # 304 → cuerpo en caché
    assert server.urls()[1:] == [f"{MEDIA}/audio/mon-laferte.wav"]
    guids = [s.meta["guid"] for s in ctx.db.list_segments(kind="music")]
    assert sorted(guids) == ["fixture-guid-003", "fixture-guid-004", "fixture-guid-005"]


def test_retired_episode_is_not_downloaded_again(tmp_path: Path, server: FakeServer) -> None:
    ctx = make_ctx(tmp_path, make_config(target_stock=1, rotate_per_run=1))
    producer = make_producer(server)
    [first] = producer.produce(ctx)
    ctx.db.update_segment_status(first.id, "retired")
    [second] = producer.produce(ctx)
    assert second.meta["guid"] != first.meta["guid"]


def test_missing_feed_url_fails_clearly(tmp_path: Path, server: FakeServer) -> None:
    cfg = make_config(feed_url=None)
    ctx = make_ctx(tmp_path, cfg)
    with pytest.raises(ProducerError, match=r"feed_url no configurado \(decisión abierta #8\)"):
        make_producer(server).produce(ctx)
    result = run_producer(ctx, make_producer(server))
    assert not result.ok and result.error == MISSING_FEED_URL
    assert server.requests == []


def test_repo_config_uses_official_npr_feed() -> None:
    """Decisión #8: solo el feed RSS oficial de audio de NPR (§7), sin recodificar."""
    settings = RadioConfig.load(REPO / "config").producers.get("music_tinydesk")
    assert settings is not None and settings.active
    assert settings.params["feed_url"] == "https://feeds.npr.org/510306/podcast.xml"
    assert settings.params["loudnorm"] is False  # términos de NPR: no modificar el contenido


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_error_leaves_stock_intact(
    tmp_path: Path, server: FakeServer, error: type[Exception]
) -> None:
    ctx = make_ctx(tmp_path, make_config(target_stock=5))
    make_producer(server).produce(ctx)
    before = {s.id: s for s in ctx.db.list_segments()}
    files = sorted((tmp_path / "stock" / "music").iterdir())

    server.fail = error
    result = run_producer(ctx, make_producer(server))
    assert not result.ok and "sin red" in str(result.error)
    assert {s.id: s for s in ctx.db.list_segments()} == before
    assert sorted((tmp_path / "stock" / "music").iterdir()) == files
    assert tmp_leftovers(tmp_path) == []


def test_download_interrupted_midway_cleans_tmp(tmp_path: Path, server: FakeServer) -> None:
    class Broken(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield wav_bytes()[:100]
            raise httpx.ReadError("corte")

    server.overrides[f"{MEDIA}/audio/anderson-paak.wav?source=rss"] = lambda r: httpx.Response(
        200, stream=Broken(), headers={"Content-Type": "audio/wav"}
    )
    ctx = make_ctx(tmp_path, make_config(target_stock=2))
    result = run_producer(ctx, make_producer(server))
    assert not result.ok and "corte" in str(result.error)
    assert ctx.db.list_segments() == []
    assert tmp_leftovers(tmp_path) == []


def test_server_error_fails_run_but_item_errors_are_skipped(
    tmp_path: Path, server: FakeServer
) -> None:
    server.overrides[f"{MEDIA}/audio/anderson-paak.wav?source=rss"] = lambda r: httpx.Response(404)
    ctx = make_ctx(tmp_path, make_config(target_stock=1))
    created = make_producer(server).produce(ctx)
    assert [s.meta["guid"] for s in created] == ["fixture-guid-004"]  # 404 y HTML descartados

    server.overrides[FEED_URL] = lambda r: httpx.Response(503)
    result = run_producer(ctx, make_producer(server))
    assert not result.ok and "503" in str(result.error)


def test_rejected_items_are_remembered(tmp_path: Path, server: FakeServer) -> None:
    ctx = make_ctx(tmp_path, make_config(target_stock=2))
    make_producer(server).produce(ctx)
    assert f"{MEDIA}/audio/roto.wav" in server.urls()
    server.requests.clear()
    make_producer(server).produce(ctx)          # otra instancia: la caché está en disco
    assert f"{MEDIA}/audio/roto.wav" not in server.urls()


def test_zero_duration_and_oversize_rejected(tmp_path: Path, server: FakeServer) -> None:
    server.overrides[f"{MEDIA}/audio/anderson-paak.wav?source=rss"] = lambda r: httpx.Response(
        200, content=wav_bytes(0.0), headers={"Content-Type": "audio/wav"}
    )
    server.overrides[f"{MEDIA}/audio/rosalia"] = lambda r: httpx.Response(
        200, content=wav_bytes(3.0), headers={"Content-Type": "audio/wav"}
    )
    ctx = make_ctx(tmp_path, make_config(target_stock=1, max_download_mb=0.03))
    created = make_producer(server).produce(ctx)
    assert [s.meta["guid"] for s in created] == ["fixture-guid-003"]
    assert ctx.stats.rejected == 3            # duración 0, HTML, demasiado grande
    assert tmp_leftovers(tmp_path) == []


def test_cache_eviction_is_lru(tmp_path: Path, server: FakeServer) -> None:
    cfg = make_config(target_stock=1, max_cache_items=2, max_per_run=1)
    ctx = make_ctx(tmp_path, cfg)
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    producer = make_producer(server)

    [a] = producer.produce(ctx)                       # t0
    clock.advance(3600)
    [b] = producer.produce(ctx)                       # t0 + 1 h
    clock.advance(3600)
    play = ctx.db.log_play_start(a.id, "music", "default", clock.now())   # A suena a t0 + 2 h
    ctx.db.log_play_end(play, clock.now())
    clock.advance(3600)
    [c] = producer.produce(ctx)                       # t0 + 3 h → 3 > 2: fuera B (LRU)

    status = {s.id: s.status for s in ctx.db.list_segments(kind="music")}
    assert status == {a.id: "ready", b.id: "retired", c.id: "ready"}
    assert not b.path.exists() and a.path.exists() and c.path.exists()


def test_evict_music_cache_by_size_spares_other_producers(tmp_path: Path) -> None:
    db = DB(":memory:")
    for i, producer in enumerate(["music_tinydesk", "music_tinydesk", "music_library"]):
        path = tmp_path / f"{i}.wav"
        path.write_bytes(b"x" * 1000)
        db.add_segment(Segment(
            id=f"s{i}", kind="music", factual=False, path=path, duration_s=1,
            created_at=NOW.replace(minute=i), producer=producer,
        ))
    report = evict_music_cache(
        db, producer="music_tinydesk", max_items=None, max_mb=1500 / 1024 / 1024,
        protect={"s0"},
    )
    assert report.retired == ["s1"] and report.freed_bytes == 1000
    assert (tmp_path / "0.wav").exists() and (tmp_path / "2.wav").exists()
    assert not (tmp_path / "1.wav").exists()
    assert {s.id: s.status for s in db.list_segments()} == {
        "s0": "ready", "s1": "retired", "s2": "ready",
    }


def test_produce_all_runs_music_by_deficit(tmp_path: Path, server: FakeServer) -> None:
    ctx = make_ctx(tmp_path, make_config(target_stock=2))
    producers = {"music_tinydesk": make_producer(server, config=ctx.config)}
    report = produce(ctx, producers=producers)       # sin cron en este config: por déficit
    assert [(r.name, r.reason, len(r.segment_ids)) for r in report.results] == [
        ("music_tinydesk", "déficit 2", 2)
    ]
    run = ctx.db.last_producer_run("music_tinydesk")
    assert run is not None and run.ok and run.n_segments == 2 and run.cost_eur == 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def _config_dir(tmp_path: Path, producers_yaml: str) -> Path:
    cfg = tmp_path / "config"
    shutil.copytree(REPO / "config", cfg)
    (cfg / "producers.yaml").write_text(producers_yaml, encoding="utf-8")
    return cfg


def test_cli_produce_dry_run(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path, """
producers:
  time_signal: {active: true, target_stock: 2}
  music_tinydesk: {active: true, target_stock: 3, params: {}}
  weather: {active: true}
""")
    data = tmp_path / "data"
    result = CliRunner().invoke(
        app, ["produce", "--all", "--dry-run", "--config-dir", str(cfg), "--data-dir", str(data)]
    )
    assert result.exit_code == 0, result.output
    assert "time_signal" in result.output and "tocaría (déficit 2)" in result.output
    assert "music_tinydesk" in result.output and "déficit 3" in result.output
    assert "weather" in result.output and "no implementado" in result.output
    with DB(data / "state.db") as db:
        assert db.list_producer_runs() == [] and db.list_segments() == []


def test_cli_produce_requires_name_or_all(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["produce", "--config-dir", str(REPO / "config")])
    assert result.exit_code == 2
    both = CliRunner().invoke(app, ["produce", "time_signal", "--all"])
    assert both.exit_code == 2


def test_cli_produce_music_without_feed_url_fails(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path, "producers:\n  music_tinydesk: {active: false}\n")
    data = tmp_path / "data"
    result = CliRunner().invoke(
        app, ["produce", "music_tinydesk", "--config-dir", str(cfg), "--data-dir", str(data)]
    )
    assert result.exit_code == 1
    assert MISSING_FEED_URL in result.output
    with DB(data / "state.db") as db:
        run = db.last_producer_run("music_tinydesk")
        assert run is not None and run.ok is False and run.error == MISSING_FEED_URL
