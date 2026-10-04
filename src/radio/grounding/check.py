"""
Grounding check de los segmentos factuales (invariante 5, §4.2 paso 3, §7, §9).

Reglas de ``check_grounding(script, claims, sources)``:

1. Cada ``claim.source_id`` existe entre las ``sources``.
2. Cada dato de cada claim aparece en la fuente que cita (o en ``allowed_terms``).
3. Cada dato del **guion** está respaldado por algún claim válido: aparece en el texto
   del claim y en la fuente que ese claim cita. Así el guion no puede colar un dato
   que no declara (aunque esté en alguna fuente). Los datos de ``allowed_terms``
   (nombre de la emisora, artista, título del episodio) no necesitan claim.

Comparación tolerante entre idiomas (guion en español, fuente en inglés o español):

- números y años por valor (``"dos mil diez"`` = ``"2010"``; ``"three"`` = ``"tres"``);
  los años de las fechas de la fuente también cuentan como años.
- fechas por componentes: ``"marzo de 1998"`` está respaldada por ``1998-03-03``
  (el guion puede ser menos preciso que la fuente, nunca más).
- nombres propios sin mayúsculas ni tildes, como frase completa entre límites de
  palabra dentro de la fuente (``"Bogota"`` ⊂ ``"Bogotá, Colombia"``). No hay
  traducción de nombres: si hace falta "Estados Unidos" frente a "United States",
  la fuente debe traer ambas formas (``radio.sources`` lo hace con los países).

Los textos de las fuentes se indexan con el léxico combinado es+en.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from radio.core.models import SourceDoc
from radio.grounding.extract import _extract, parse_date_norm
from radio.grounding.lexicon import fold
from radio.grounding.models import Claim, Fact, GroundingReport

# Idiomas con los que se indexan las fuentes (MusicBrainz en español, Wikipedia es/en)
SOURCE_LANGS: tuple[str, ...] = ("es", "en")

_Date = tuple[int | None, int | None, int | None]


@dataclass(frozen=True)
class _Index:
    """Lo comparable de un texto: texto plegado, valores numéricos y fechas."""
    folded: str
    numbers: frozenset[Decimal]
    dates: frozenset[_Date]

    @classmethod
    def build(cls, texts: Iterable[str], langs: tuple[str, ...]) -> _Index:
        texts = list(texts)
        numbers: set[Decimal] = set()
        dates: set[_Date] = set()
        for text in texts:
            for fact in _extract(text, langs, want_names=False):
                if fact.kind == "date":
                    parsed = parse_date_norm(fact.normalized)
                    dates.add(parsed)
                    if parsed[0] is not None:
                        numbers.add(Decimal(parsed[0]))
                else:
                    numbers.add(Decimal(fact.normalized))
        folded = "\n".join(fold(t) for t in texts)
        return cls(folded, frozenset(numbers), frozenset(dates))

    def supports(self, fact: Fact) -> bool:
        if fact.kind in ("number", "year"):
            return Decimal(fact.normalized) in self.numbers
        if fact.kind == "date":
            want = parse_date_norm(fact.normalized)
            return any(_date_covers(have, want) for have in self.dates)
        return _phrase_re(fact.normalized).search(self.folded) is not None


def _date_covers(have: _Date, want: _Date) -> bool:
    return all(w is None or w == h for w, h in zip(want, have, strict=True))


def _phrase_re(normalized: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in normalized.split()]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)")


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def check_grounding(
    script: str,
    claims: Sequence[Claim],
    sources: Sequence[SourceDoc],
    *,
    lang: str = "es",
    allowed_terms: Sequence[str] = (),
) -> GroundingReport:
    """
    Verifica que todo dato del guion y de los claims venga de las fuentes citadas.

    ``lang`` es el idioma del guion y de los claims. ``allowed_terms`` son términos
    siempre permitidos sin fuente (nombre de la emisora, nombre del artista, título
    del episodio como identificador). Nunca lanza por datos malos: todo va a
    ``GroundingReport.problems``.
    """
    problems: list[str] = []
    unsupported: list[str] = []

    by_id: dict[str, SourceDoc] = {}
    for doc in sources:
        if doc.id in by_id:
            problems.append(f"fuente duplicada: {doc.id!r}")
            continue
        by_id[doc.id] = doc
    source_index = {sid: _Index.build([doc.text], SOURCE_LANGS) for sid, doc in by_id.items()}
    allowed = _Index.build(allowed_terms, (lang, *SOURCE_LANGS))
    everything = _Index.build([doc.text for doc in by_id.values()], SOURCE_LANGS)

    # 1 y 2: cada claim cita una fuente real y sus datos están en ella
    valid_claims: list[tuple[_Index, _Index]] = []   # (índice del claim, índice de su fuente)
    for n, claim in enumerate(claims, start=1):
        src = source_index.get(claim.source_id)
        if src is None:
            problems.append(f"claim {n} cita una fuente inexistente: {claim.source_id!r}")
            continue
        claim_ok = True
        for fact in _extract(claim.text, (lang,)):
            if src.supports(fact) or allowed.supports(fact):
                continue
            claim_ok = False
            unsupported.append(fact.text)
            problems.append(
                f"claim {n}: «{fact.text}» no aparece en la fuente {claim.source_id!r}"
            )
        if claim_ok:
            valid_claims.append((_Index.build([claim.text], (lang,)), src))

    # 3: cada dato del guion está respaldado por un claim válido
    script_facts = _extract(script, (lang,))
    for fact in script_facts:
        if allowed.supports(fact):
            continue
        if any(ci.supports(fact) and si.supports(fact) for ci, si in valid_claims):
            continue
        unsupported.append(fact.text)
        if everything.supports(fact):
            problems.append(f"el guion afirma «{fact.text}» sin un claim que lo respalde")
        else:
            problems.append(f"el guion afirma «{fact.text}», que no aparece en ninguna fuente")

    return GroundingReport(
        ok=not problems,
        problems=tuple(problems),
        unsupported=_dedupe(unsupported),
        checked_facts=tuple(script_facts),
    )


def script_has_facts(
    script: str, *, lang: str = "es", allowed_terms: Sequence[str] = ()
) -> bool:
    """
    True si el guion contiene algún dato verificable fuera de ``allowed_terms``.
    Sirve para aceptar la versión sin dato (invariante 5): un guion sin datos no
    necesita fuentes ni claims.
    """
    allowed = _Index.build(allowed_terms, (lang, *SOURCE_LANGS))
    return any(not allowed.supports(f) for f in _extract(script, (lang,)))
