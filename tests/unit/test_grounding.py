"""
Tests de ``radio.grounding`` (invariante 5, §4.2 paso 3, §9 de ARCHITECTURE.md).

Las fuentes son sintéticas: una artista inventada ("Nube Ferrán") con una ficha tipo
MusicBrainz en español y un resumen tipo Wikipedia en inglés.
"""

from __future__ import annotations

import itertools

import pytest

from radio.core.models import SourceDoc
from radio.grounding import (
    Claim,
    Fact,
    GroundingReport,
    check_grounding,
    extract_facts,
    fold,
    script_has_facts,
)

MB = SourceDoc(
    id="mb:00000000-0000-4000-8000-000000000001",
    text=(
        "Nombre: Nube Ferrán\n"
        "Tipo: persona (Person)\n"
        "País: Colombia (Colombia, CO)\n"
        "Zona de origen: Bogotá\n"
        "Inicio: 1998-03-03\n"
    ),
    url="https://musicbrainz.org/artist/00000000-0000-4000-8000-000000000001",
)
WP = SourceDoc(
    id="wp:en:Nube_Ferrán",
    text=(
        "Nube Ferrán (born 3 March 1998) is a Colombian singer-songwriter from Bogotá. "
        "She has released three studio albums; the second, Lluvia Lenta, came out in 2019 "
        "and sold 10,000 copies. She moved to Valparaíso in 2021."
    ),
    url="https://en.wikipedia.org/wiki/Nube_Ferr%C3%A1n",
)
SOURCES = [MB, WP]
ALLOWED = ("Radio Parra", "Nube Ferrán", "Nube Ferrán: Tiny Desk Concert")


def check(script: str, claims: list[Claim]) -> GroundingReport:
    return check_grounding(script, claims, SOURCES, lang="es", allowed_terms=ALLOWED)


def kinds(text: str, lang: str = "es") -> list[tuple[str, str]]:
    return [(f.kind, f.normalized) for f in extract_facts(text, lang)]


# ── §9: el test de aceptación ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("invented", "fact_text"),
    [
        ("Nube Ferrán nació en 1995.", "1995"),                      # año inventado
        ("Nube Ferrán nació en Medellín.", "Medellín"),              # ciudad inventada
        ("Nube Ferrán ha publicado cinco discos.", "cinco"),          # número inventado
        ("Nube Ferrán nació el 4 de marzo de 1998.", "4 de marzo de 1998"),  # fecha
    ],
)
def test_grounding_rejects_fact_absent_from_sources(invented: str, fact_text: str) -> None:
    """§9: un guion con un dato que no está en las fuentes debe ser rechazado."""
    # El LLM incluso "cita" una fuente para el dato inventado: no basta.
    claims = [Claim(invented, WP.id)]
    report = check(f"Desde Bogotá, {invented} Esto es Radio Parra.", claims)
    assert not report.ok
    assert fact_text in report.unsupported
    assert any(fact_text in p for p in report.problems)

    # Y sin claim ninguno, también.
    bare = check(invented, [])
    assert not bare.ok
    assert fact_text in bare.unsupported


# ── Aceptación y claims ──────────────────────────────────────────────────────

def test_accepts_script_whose_facts_are_all_in_cited_sources() -> None:
    script = (
        "En Radio Parra suena Nube Ferrán. Nació en Bogotá en marzo de 1998 y tiene "
        "tres discos; el segundo, Lluvia Lenta, salió en dos mil diecinueve."
    )
    claims = [
        Claim("Nube Ferrán nació en Bogotá", MB.id),
        Claim("Nació en marzo de 1998", MB.id),
        Claim("Tiene tres discos; el segundo, Lluvia Lenta, salió en 2019", WP.id),
    ]
    report = check(script, claims)
    assert report.ok, report.problems
    assert report.unsupported == ()
    normalized = {f.normalized for f in report.checked_facts}
    assert {"bogota", "1998-03", "3", "lluvia lenta", "2019"} <= normalized


def test_claim_citing_unknown_source_is_rejected() -> None:
    report = check("Nube Ferrán nació en Bogotá.", [Claim("Nació en Bogotá", "wp:es:Inventada")])
    assert not report.ok
    assert any("fuente inexistente" in p and "wp:es:Inventada" in p for p in report.problems)
    assert "Bogotá" in report.unsupported


def test_claim_citing_wrong_source_is_rejected() -> None:
    # 2019 está en Wikipedia, no en la ficha de MusicBrainz que cita el claim
    report = check("Su segundo disco salió en 2019.", [Claim("El segundo disco salió en 2019", MB.id)])
    assert not report.ok
    assert any(f"no aparece en la fuente {MB.id!r}" in p for p in report.problems)
    assert "2019" in report.unsupported


def test_unclaimed_fact_in_script_is_rejected_even_if_in_a_source() -> None:
    # Valparaíso sí está en una fuente, pero ningún claim lo declara
    claims = [Claim("Nube Ferrán nació en Bogotá", MB.id)]
    report = check("Nube Ferrán nació en Bogotá y vive en Valparaíso.", claims)
    assert not report.ok
    assert report.unsupported == ("Valparaíso",)
    assert any("sin un claim" in p for p in report.problems)


def test_script_fact_must_match_the_claim_not_just_any_claim() -> None:
    # El claim declara 1998 pero el guion dice 2021 (que está en WP, sin claim)
    report = check("Se mudó en 2021.", [Claim("Nació en 1998", MB.id)])
    assert not report.ok
    assert "2021" in report.unsupported


def test_duplicate_source_ids_are_a_problem() -> None:
    report = check_grounding("Hola.", [], [MB, MB])
    assert not report.ok
    assert any("duplicada" in p for p in report.problems)


def test_claim_from_dict() -> None:
    assert Claim.from_dict({"text": "x", "source_id": "mb:1"}) == Claim("x", "mb:1")
    with pytest.raises(ValueError):
        Claim.from_dict({"text": "x"})


# ── Extracción ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("grabó dos canciones", [("number", "2")]),
        ("treinta y dos temas", [("number", "32")]),
        ("en dos mil diez", [("year", "2010")]),
        ("en mil novecientos noventa y ocho", [("year", "1998")]),
        ("dos millones trescientos mil oyentes", [("number", "2300000")]),
        ("ciento veinte conciertos", [("number", "120")]),
        ("veintiún días", [("number", "21")]),
        ("dieciséis y diecisiete", [("number", "16"), ("number", "17")]),
        ("su tercer disco", [("number", "3")]),
        ("la segunda gira", [("number", "2")]),
        ("un disco y una gira", []),                 # artículos, no números
        ("por primera vez", []),
        ("un millón de escuchas", [("number", "1000000")]),
    ],
)
def test_spanish_number_words(text: str, expected: list[tuple[str, str]]) -> None:
    assert kinds(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.000 copias", [("number", "10000")]),
        ("10,000 copies", [("number", "10000")]),
        ("1,5 horas", [("number", "1.5")]),
        ("3º puesto", [("number", "3")]),
        ("en 1998", [("year", "1998")]),
        ("los 1990s", [("year", "1990")]),
        ("7 temas", [("number", "7")]),
    ],
)
def test_digit_numbers(text: str, expected: list[tuple[str, str]]) -> None:
    assert kinds(text) == expected


@pytest.mark.parametrize(
    ("text", "lang", "normalized"),
    [
        ("el 3 de marzo de 1998", "es", "1998-03-03"),
        ("el 1 de enero del 2000", "es", "2000-01-01"),
        ("en marzo de 1998", "es", "1998-03"),
        ("el 3 de marzo", "es", "--03-03"),
        ("en septiembre", "es", "--09"),
        ("Marzo de 1998", "es", "1998-03"),
        ("1998-03-03", "es", "1998-03-03"),
        ("1998-03", "es", "1998-03"),
        ("March 3, 1998", "en", "1998-03-03"),
        ("3 March 1998", "en", "1998-03-03"),
        ("born in March 1998", "en", "1998-03"),
        ("in May", "en", "--05"),
    ],
)
def test_dates_es_en(text: str, lang: str, normalized: str) -> None:
    dates = [f for f in extract_facts(text, lang) if f.kind == "date"]
    assert [d.normalized for d in dates] == [normalized]
    assert all(f.kind != "number" for f in extract_facts(text, lang))


def test_english_lowercase_may_is_not_a_month() -> None:
    assert kinds("you may go", "en") == []


def test_spanish_date_matches_english_source() -> None:
    report = check("Nació el 3 de marzo de 1998.", [Claim("Nació el 3 de marzo de 1998", WP.id)])
    assert report.ok, report.problems


def test_less_precise_date_is_covered_but_more_precise_is_not() -> None:
    assert check("Nació en marzo.", [Claim("Nació en marzo", MB.id)]).ok
    assert check("Nació en 1998.", [Claim("Nació en 1998", MB.id)]).ok
    src = SourceDoc("s", "Released in March 2019.", "")
    bad = check_grounding("Salió el 3 de marzo de 2019.", [Claim("Salió el 3 de marzo de 2019", "s")], [src])
    assert not bad.ok


def test_proper_nouns_and_stopwords() -> None:
    facts = extract_facts(
        "Hoy escuchamos a Nube Ferrán. Desde Bogotá llega la Orquesta del Sol. "
        "El lunes, en Ciudad de México."
    )
    names = [f.normalized for f in facts if f.kind == "proper_noun"]
    assert names == ["nube ferran", "bogota", "orquesta del sol", "ciudad de mexico"]


def test_sentence_initial_word_is_a_name_unless_common() -> None:
    names = [f.normalized for f in extract_facts("Madrid la vio nacer. Vamos allá.")
             if f.kind == "proper_noun"]
    assert names == ["madrid"]
    # aparece en minúscula en el texto → palabra común
    assert not script_has_facts("Tarde de música. Una tarde tranquila.")
    assert not script_has_facts("Precisamente eso. Seguimos.")


def test_accents_and_case_are_ignored_for_names() -> None:
    assert fold("Bogotá ÑANDÚ") == "bogota nandu"
    report = check("Nube Ferran nacio en BOGOTA.", [Claim("Nacio en BOGOTA", MB.id)])
    assert report.ok, report.problems


def test_partial_name_is_covered_but_longer_name_is_not() -> None:
    # una parte de un nombre de la fuente vale ("Ferrán" ⊂ "Nube Ferrán")
    assert check("Escuchamos a Ferrán.", [Claim("Escuchamos a Ferrán", MB.id)]).ok
    assert check("Suena Lluvia Lenta.", [Claim("Lluvia Lenta", WP.id)]).ok
    report = check("El disco Lluvia Lenta Nocturna.", [Claim("Lluvia Lenta Nocturna", WP.id)])
    assert not report.ok


def test_allowed_terms_need_no_source() -> None:
    script = "Esto es Radio Parra y suena Nube Ferrán en su Tiny Desk Concert."
    report = check_grounding(script, [], [], allowed_terms=ALLOWED)
    assert report.ok, report.problems
    assert not script_has_facts(script, allowed_terms=ALLOWED)
    assert script_has_facts(script)   # sin allowed_terms sí hay nombres propios


def test_fact_free_script_is_accepted() -> None:
    script = "Y ahora, un poco de música tranquila. Disfruten de la canción."
    assert not script_has_facts(script)
    report = check_grounding(script, [], [])
    assert report == GroundingReport(ok=True, problems=(), unsupported=(), checked_facts=())


def test_bilingual_country_names_come_from_the_source() -> None:
    src = SourceDoc("mb:x", "País: Estados Unidos (United States, EE. UU., US)", "")
    for script in ("Viene de Estados Unidos.", "Viene de EE. UU."):
        report = check_grounding(script, [Claim(script, "mb:x")], [src])
        assert report.ok, report.problems
    report = check_grounding("Viene de Canadá.", [Claim("Viene de Canadá", "mb:x")], [src])
    assert not report.ok


def test_unsupported_language_uses_digits_and_names_only() -> None:
    facts = extract_facts("La Nube Ferrán va néixer el 1998 a Bogotà.", lang="ca")
    assert ("year", "1998") in [(f.kind, f.normalized) for f in facts]
    assert any(f.kind == "proper_noun" and f.normalized == "bogota" for f in facts)


def test_fact_dataclass_is_frozen() -> None:
    fact = Fact("year", "1998", "1998")
    with pytest.raises(AttributeError):
        fact.text = "x"  # type: ignore[misc]


# ── Propiedades sobre un corpus pequeño ──────────────────────────────────────

# (frase, fuente que la respalda) — todas ciertas según SOURCES
TRUE_SENTENCES = [
    ("Nube Ferrán nació en Bogotá.", MB.id),
    ("Nació el 3 de marzo de 1998.", WP.id),
    ("Es de Colombia.", MB.id),
    ("Ha publicado tres discos.", WP.id),
    ("Su segundo disco salió en 2019.", WP.id),
    ("Vendió 10.000 copias.", WP.id),
    ("En dos mil veintiuno se mudó a Valparaíso.", WP.id),
]
# Frases con un dato que no está en ninguna fuente
FALSE_SENTENCES = [
    "Nació en 1997.",
    "Ha publicado cuatro discos.",
    "Vive en Lima.",
    "Vendió 20.000 copias.",
    "Su gira empezó el 5 de mayo de 2022.",
    "Tocó con Rosa Almendro.",
    "Grabó en diciembre de 2018.",
]


@pytest.mark.parametrize("pair", list(itertools.combinations(range(len(TRUE_SENTENCES)), 2)))
def test_property_true_sentences_with_their_claims_pass(pair: tuple[int, int]) -> None:
    chosen = [TRUE_SENTENCES[i] for i in pair]
    script = " ".join(s for s, _ in chosen) + " Suena en Radio Parra."
    claims = [Claim(s, sid) for s, sid in chosen]
    report = check(script, claims)
    assert report.ok, report.problems


@pytest.mark.parametrize(
    ("true_idx", "false_idx"),
    list(itertools.product(range(len(TRUE_SENTENCES)), range(len(FALSE_SENTENCES)))),
)
def test_property_any_invented_fact_is_rejected(true_idx: int, false_idx: int) -> None:
    good, sid = TRUE_SENTENCES[true_idx]
    bad = FALSE_SENTENCES[false_idx]
    script = f"{good} {bad}"
    # peor caso: el LLM cita cada frase, incluida la inventada, contra todas las fuentes
    claims = [Claim(good, sid)] + [Claim(bad, s.id) for s in SOURCES]
    report = check(script, claims)
    assert not report.ok
    assert report.unsupported


@pytest.mark.parametrize("sentence", [s for s, _ in TRUE_SENTENCES])
def test_property_true_sentence_without_claim_is_rejected(sentence: str) -> None:
    assert script_has_facts(sentence, allowed_terms=ALLOWED)
    assert not check(sentence, []).ok


def test_property_extraction_is_deterministic() -> None:
    text = " ".join(s for s, _ in TRUE_SENTENCES) + " " + " ".join(FALSE_SENTENCES)
    assert extract_facts(text) == extract_facts(text)
