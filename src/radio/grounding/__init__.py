"""
Grounding: extracción y verificación de datos de los segmentos factuales
(invariante 5, §4.2 paso 3, §7 y §9 de ARCHITECTURE.md).

Paquete puro y determinista, sin I/O. Uso típico en la etapa *validate* de un
productor factual (p. ej. ``host_intro``)::

    from radio.grounding import Claim, check_grounding, script_has_facts

    claims = [Claim.from_dict(c) for c in draft_json["claims"]]
    report = check_grounding(
        script, claims, sources,
        lang="es",
        allowed_terms=(station_name, artist, episode_title),
    )
    if not report.ok:
        # reintento con prompt más estricto (report.problems) o versión sin dato;
        # una versión sin dato se acepta si not script_has_facts(script, ...)
        ...

Qué se considera dato: números (cifras y palabras), años, fechas y nombres propios
(``Fact.kind``). Lo demás (géneros, adjetivos) no se verifica aquí.
"""

from radio.grounding.check import SOURCE_LANGS, check_grounding, script_has_facts
from radio.grounding.extract import extract_facts
from radio.grounding.lexicon import fold
from radio.grounding.models import FACT_KINDS, Claim, Fact, FactKind, GroundingReport

__all__ = [
    "FACT_KINDS",
    "SOURCE_LANGS",
    "Claim",
    "Fact",
    "FactKind",
    "GroundingReport",
    "check_grounding",
    "extract_facts",
    "fold",
    "script_has_facts",
]
