"""
Tests de ``radio.sources`` (gather de ``host_intro``, §4.2 paso 1, §12 Fase 2).

Sin red: ``httpx.MockTransport`` sirve las respuestas sintéticas de
``tests/fixtures/sources/`` (artista ficticia "Nube Ferrán"). El reloj y ``sleep``
son falsos, así que las esperas del límite de tasa y del backoff se comprueban
sin dormir de verdad.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from radio.core.models import SourceDoc
from radio.grounding import Claim, check_grounding
from radio.music.feed import USER_AGENT
from radio.sources import (
    Fetcher,
    RateLimiter,
    SourceCache,
    SourceError,
    gather_artist_sources,
    source_license,
    source_meta,
    sources_cache_dir,
)
from radio.sources.countries import country_names, spanish_name_for
from radio.sources.musicbrainz import MatchStatus, names_match, select_match
from radio.sources.wikipedia import is_acceptable_title, summary_doc

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sources"
MBID = "5e1f0a3c-0000-4000-8000-00000000f001"
ARTIST = "Nube Ferrán"

MB_SEARCH = "musicbrainz.org/ws/2/artist/"
MB_LOOKUP = f"musicbrainz.org/ws/2/artist/{MBID}"
WIKIDATA = "www.wikidata.org/w/api.php"
WP_ES_SUMMARY = "es.wikipedia.org/api/rest_v1/page/summary/Nube_Ferrán"
WP_EN_SUMMARY = "en.wikipedia.org/api/rest_v1/page/summary/Nube_Ferrán_(singer)"
WP_ES_SEARCH = "es.wikipedia.org/w/api.php"
WP_EN_SEARCH = "en.wikipedia.org/w/api.php"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTime:
    """Reloj monótono falso; ``sleep`` avanza el reloj y queda registrado."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# Una ruta responde con: nombre de fixture (200), código HTTP, (código, cabeceras),
# excepción de httpx, o una lista de lo anterior (una por petición; la última se repite).
Reply = str | int | tuple[int, dict[str, str]] | Exception
RouteValue = Reply | list[Reply]


class Router:
    """Transporte falso: enruta por ``host + path`` y registra (hora, petición)."""

    def __init__(self, clock: FakeTime, routes: dict[str, RouteValue]) -> None:
        self.clock = clock
        self.routes = dict(routes)
        self.requests: list[tuple[float, httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.clock.now, request))
        key = f"{request.url.host}{request.url.path}"
        value = self.routes.get(key, 404)
        if isinstance(value, list):
            reply = value.pop(0) if len(value) > 1 else value[0]
        else:
            reply = value
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return httpx.Response(200, json=load(reply))
        if isinstance(reply, tuple):
            return httpx.Response(reply[0], headers=reply[1])
        return httpx.Response(reply)

    def hits(self, key: str) -> list[tuple[float, httpx.Request]]:
        return [(t, r) for t, r in self.requests if f"{r.url.host}{r.url.path}" == key]


HAPPY: dict[str, RouteValue] = {
    MB_SEARCH: "mb_search_nube.json",
    MB_LOOKUP: "mb_artist_nube.json",
    WIKIDATA: "wd_entities_nube.json",
    WP_ES_SUMMARY: "wp_es_summary_nube.json",
    WP_EN_SUMMARY: "wp_en_summary_nube.json",
}


def run(
    routes: dict[str, RouteValue],
    *,
    cache: SourceCache | None = None,
    artist: str = ARTIST,
    lang: str = "es",
) -> tuple[list[SourceDoc], Router, FakeTime]:
    clock = FakeTime()
    router = Router(clock, routes)
    with httpx.Client(transport=httpx.MockTransport(router)) as client:
        docs = gather_artist_sources(
            artist, client=client, cache=cache, lang=lang,
            clock=clock.monotonic, sleep=clock.sleep,
        )
    return docs, router, clock


# ── gather_artist_sources ────────────────────────────────────────────────────

def test_happy_path_returns_musicbrainz_and_spanish_wikipedia() -> None:
    docs, router, _ = run(HAPPY)
    assert [d.id for d in docs] == [f"mb:{MBID}", "wp:es:Nube_Ferrán"]
    mb, wp = docs
    assert mb.url == f"https://musicbrainz.org/artist/{MBID}"
    assert "Nombre: Nube Ferrán" in mb.text
    assert "Tipo: persona (Person)" in mb.text
    assert "País: Estados Unidos, United States, EE. UU., EEUU, USA, US" in mb.text
    assert "Zona de origen: Villa Parral" in mb.text
    assert "Nacimiento: 1991-05-17" in mb.text
    assert "Aclaración: cantautora ficticia de pruebas" in mb.text
    assert wp.url == "https://es.wikipedia.org/wiki/Nube_Ferr%C3%A1n"
    assert wp.text.startswith("Nube Ferrán (Villa Parral, 17 de mayo de 1991)")
    # sin búsqueda en Wikipedia: el título viene de MusicBrainz → Wikidata
    assert router.hits(WP_ES_SEARCH) == []
    # búsqueda de MusicBrainz bien formada; lookup solo con relaciones de URL (CC0)
    search = router.hits(MB_SEARCH)[0][1]
    assert search.url.params["query"] == 'artist:"Nube Ferrán"'
    assert search.url.params["fmt"] == "json"
    lookup = router.hits(MB_LOOKUP)[0][1]
    assert lookup.url.params["inc"] == "url-rels"


def test_user_agent_on_every_request() -> None:
    _, router, _ = run(HAPPY)
    assert len(router.requests) == 4
    for _, request in router.requests:
        assert request.headers["User-Agent"] == USER_AGENT
        assert "RadioParra" in request.headers["User-Agent"]


def test_musicbrainz_rate_limit_one_request_per_second() -> None:
    _, router, clock = run(HAPPY)
    mb_times = [t for t, _ in router.hits(MB_SEARCH) + router.hits(MB_LOOKUP)]
    assert len(mb_times) == 2
    assert mb_times[1] - mb_times[0] >= 1.0
    assert clock.sleeps[0] == pytest.approx(1.0)   # la espera antes del lookup
    # otros hosts no esperan por MusicBrainz
    assert sum(clock.sleeps) == pytest.approx(1.0)


def test_rate_limiter_is_per_host_and_shared() -> None:
    clock = FakeTime()
    limiter = RateLimiter(clock=clock.monotonic, sleep=clock.sleep)
    limiter.wait("musicbrainz.org")
    limiter.wait("es.wikipedia.org")
    limiter.wait("es.wikipedia.org")
    clock.now += 0.3
    limiter.wait("musicbrainz.org")
    assert clock.sleeps == [pytest.approx(0.5), pytest.approx(0.2)]


def test_ambiguous_artist_returns_nothing() -> None:
    routes = dict(HAPPY, **{MB_SEARCH: "mb_search_ambiguous.json"})
    docs, router, _ = run(routes)
    assert docs == []
    assert len(router.requests) == 1       # ni lookup ni Wikipedia


def test_spanish_wikipedia_missing_falls_back_to_english() -> None:
    routes = dict(HAPPY, **{WP_ES_SUMMARY: 404})
    docs, _, _ = run(routes)
    assert [d.id for d in docs] == [f"mb:{MBID}", "wp:en:Nube_Ferrán_(singer)"]
    assert docs[1].url == "https://en.wikipedia.org/wiki/Nube_Ferr%C3%A1n_(singer)"
    assert "born May 17, 1991" in docs[1].text


def test_disambiguation_page_is_rejected_and_search_is_strict() -> None:
    # MusicBrainz no conoce a la artista → búsqueda en Wikipedia
    routes: dict[str, RouteValue] = {
        MB_SEARCH: "mb_search_empty.json",
        WP_ES_SEARCH: "wp_es_search_nube.json",
        WP_ES_SUMMARY: "wp_es_disambiguation.json",
        WP_EN_SEARCH: "wp_en_search_nube.json",
        WP_EN_SUMMARY: "wp_en_summary_nube.json",
    }
    docs, router, _ = run(routes)
    # es: "Faro de Niebla" y "(futbolista)" no valen; "Nube Ferrán" es desambiguación
    assert [d.id for d in docs] == ["wp:en:Nube_Ferrán_(singer)"]
    summaries = [r.url.path for _, r in router.requests if "/page/summary/" in r.url.path]
    assert summaries == [
        "/api/rest_v1/page/summary/Nube_Ferrán",
        "/api/rest_v1/page/summary/Nube_Ferrán_(singer)",
    ]


def test_503_is_retried_with_backoff() -> None:
    routes = dict(HAPPY, **{MB_SEARCH: [(503, {"Retry-After": "3"}), 503, "mb_search_nube.json"]})
    docs, router, clock = run(routes)
    assert [d.id for d in docs][0] == f"mb:{MBID}"
    assert len(router.hits(MB_SEARCH)) == 3
    assert clock.sleeps[:2] == [3.0, 4.0]   # Retry-After, luego 2 s · 2^1


def test_persistent_503_skips_musicbrainz_without_raising() -> None:
    routes: dict[str, RouteValue] = {
        MB_SEARCH: 503,
        WP_ES_SEARCH: "wp_es_search_nube.json",
        WP_ES_SUMMARY: "wp_es_summary_nube.json",
    }
    docs, router, _ = run(routes)
    # sin MusicBrainz, Wikipedia por búsqueda estricta
    assert [d.id for d in docs] == ["wp:es:Nube_Ferrán"]
    assert len(router.hits(MB_SEARCH)) == 4    # 1 + 3 reintentos


def test_network_error_returns_empty_list() -> None:
    routes: dict[str, RouteValue] = {
        key: httpx.ConnectError("sin red")
        for key in (MB_SEARCH, WP_ES_SEARCH, WP_EN_SEARCH)
    }
    docs, _, _ = run(routes)
    assert docs == []


def test_malformed_json_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>no es json</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        clock = FakeTime()
        assert gather_artist_sources(ARTIST, client=client, clock=clock.monotonic,
                                     sleep=clock.sleep) == []


def test_empty_artist_makes_no_requests() -> None:
    docs, router, _ = run(HAPPY, artist="   ")
    assert docs == [] and router.requests == []


def test_cache_hit_avoids_requests(tmp_path: Path) -> None:
    wall = [1_000_000.0]
    cache = SourceCache(tmp_path / "cache", now=lambda: wall[0])
    first, router1, _ = run(HAPPY, cache=cache)
    assert len(router1.requests) == 4
    second, router2, clock2 = run(HAPPY, cache=cache)
    assert second == first
    assert router2.requests == [] and clock2.sleeps == []
    # caducada la entrada (TTL 30 días) se vuelve a pedir
    wall[0] += 31 * 24 * 3600
    _, router3, _ = run(HAPPY, cache=cache)
    assert len(router3.requests) == 4


def test_cache_remembers_404(tmp_path: Path) -> None:
    cache = SourceCache(tmp_path)
    routes = dict(HAPPY, **{WP_ES_SUMMARY: 404})
    run(routes, cache=cache)
    _, router, _ = run(routes, cache=cache)
    assert router.requests == []


# ── Piezas sueltas ───────────────────────────────────────────────────────────

def test_source_cache_roundtrip_expiry_and_corruption(tmp_path: Path) -> None:
    wall = [100.0]
    cache = SourceCache(tmp_path, ttl_s=10, now=lambda: wall[0])
    assert cache.get("https://x.invalid/a") is None
    cache.put("https://x.invalid/a", 200, {"k": "ñ"})
    hit = cache.get("https://x.invalid/a")
    assert hit is not None and hit.body == {"k": "ñ"} and hit.status == 200
    wall[0] += 11
    assert cache.get("https://x.invalid/a") is None
    for path in tmp_path.glob("*.json"):
        path.write_text("{roto", encoding="utf-8")
    wall[0] = 100.0
    assert cache.get("https://x.invalid/a") is None
    assert sources_cache_dir(Path("data")) == Path("data/cache/sources")
    assert SourceCache.for_data_dir(tmp_path).root == tmp_path / "cache" / "sources"


def test_fetcher_raises_source_error_on_unexpected_status() -> None:
    clock = FakeTime()
    router = Router(clock, {"x.invalid/a": 418})
    with httpx.Client(transport=httpx.MockTransport(router)) as client:
        fetcher = Fetcher(client, limiter=RateLimiter(clock=clock.monotonic, sleep=clock.sleep))
        with pytest.raises(SourceError):
            fetcher.get_json("https://x.invalid/a")
        assert fetcher.get_json("https://x.invalid/nada") is None   # 404


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Nube Ferrán", "nube ferran", True),
        ("The Parra Band", "Parra Band", True),
        ("Nube & Ferrán", "Nube and Ferrán", True),
        ("Nube Ferrán", "Nube Ferrari Trío", False),
        ("Nube", "Nube Ferrán", False),
    ],
)
def test_names_match(a: str, b: str, same: bool) -> None:
    assert names_match(a, b) is same


def test_select_match_requires_score_and_name() -> None:
    payload = load("mb_search_nube.json")
    assert select_match(payload, ARTIST).status is MatchStatus.FOUND
    assert select_match(payload, "Nube Ferrari Trío").status is MatchStatus.NOT_FOUND  # score 61
    assert select_match(load("mb_search_ambiguous.json"), ARTIST).status is MatchStatus.AMBIGUOUS
    assert select_match({"artists": "raro"}, ARTIST).status is MatchStatus.NOT_FOUND


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Nube Ferrán", True),
        ("Nube Ferrán (cantante)", True),
        ("Nube Ferrán (singer)", True),
        ("Nube Ferrán (futbolista)", False),
        ("Nube Ferrán en directo", False),
        ("Faro de Niebla", False),
    ],
)
def test_wikipedia_title_check(title: str, ok: bool) -> None:
    assert is_acceptable_title(title, ARTIST) is ok


def test_summary_doc_rejects_disambiguation_and_empty() -> None:
    assert summary_doc(load("wp_es_disambiguation.json"), "es") is None
    assert summary_doc({"type": "standard", "title": "X", "extract": ""}, "es") is None
    doc = summary_doc(load("wp_es_summary_nube.json"), "es")
    assert doc is not None and doc.id == "wp:es:Nube_Ferrán"


def test_country_names_are_bilingual() -> None:
    assert country_names("mx") == ("México", "Mexico", "MX")
    assert country_names("ZZ") == ("ZZ",)
    assert spanish_name_for("Spain") == "España"


def test_license_notes() -> None:
    mb = SourceDoc(f"mb:{MBID}", "x", "u")
    wp = SourceDoc("wp:es:X", "x", "u")
    mb_license = source_license(mb)
    assert mb_license is not None and "CC0" in mb_license
    assert source_license(wp) == "CC BY-SA 4.0"
    assert source_license(SourceDoc("reloj", "x", "")) is None
    assert source_meta(wp) == {"id": "wp:es:X", "url": "u", "text": "x",
                               "license": "CC BY-SA 4.0"}


# ── De punta a punta con el grounding ────────────────────────────────────────

def test_gathered_sources_ground_a_spanish_script() -> None:
    docs, _, _ = run(HAPPY)
    mb_id, wp_id = docs[0].id, docs[1].id
    allowed = ("Radio Parra", ARTIST, f"{ARTIST}: Tiny Desk Concert")
    script = (
        "En Radio Parra, Nube Ferrán, que nació en Villa Parral en mayo de 1991 y es de "
        "Estados Unidos. Su segundo disco, Faro de Niebla, salió en 2018."
    )
    claims = [
        Claim("Nació en Villa Parral en mayo de 1991", mb_id),
        Claim("Es de Estados Unidos", mb_id),
        Claim("Su segundo disco, Faro de Niebla, salió en 2018", wp_id),
    ]
    report = check_grounding(script, claims, docs, allowed_terms=allowed)
    assert report.ok, report.problems
    invented = script.replace("2018", "2017")
    bad = check_grounding(invented, [*claims[:2], Claim("salió en 2017", wp_id)], docs,
                          allowed_terms=allowed)
    assert not bad.ok and "2017" in bad.unsupported
