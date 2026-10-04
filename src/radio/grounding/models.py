"""
Modelos del grounding check (§4.2 paso 3 de ARCHITECTURE.md).

- ``Fact``: dato verificable extraído de un texto (número, año, fecha o nombre propio).
- ``Claim``: afirmación del guion que el LLM declara junto con la fuente que la respalda
  (``claims: [{text, source_id}]`` del JSON de la etapa *write*).
- ``GroundingReport``: veredicto de ``check_grounding``.

Todos son inmutables (``frozen=True``) y sin I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

# Clases de dato verificable que reconoce el extractor
FactKind = Literal["number", "year", "date", "proper_noun"]
FACT_KINDS: frozenset[str] = frozenset(get_args(FactKind))


@dataclass(frozen=True)
class Fact:
    """
    Dato verificable encontrado en un texto.

    - ``text``: fragmento tal cual aparece (p. ej. ``"dos mil diez"``, ``"3 de marzo"``).
    - ``normalized``: forma comparable entre idiomas:

      * ``number`` / ``year``: valor decimal canónico (``"2010"``, ``"1.5"``).
      * ``date``: ISO parcial: ``"1998-03-03"``, ``"1998-03"``, ``"--03-03"`` (sin año)
        o ``"--03"`` (solo mes).
      * ``proper_noun``: minúsculas sin tildes y espacios simples (``"bogota"``).
    """
    kind: FactKind
    text: str
    normalized: str


@dataclass(frozen=True)
class Claim:
    """Afirmación del guion y el ``SourceDoc.id`` que la respalda."""
    text: str
    source_id: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Claim:
        """Construye un ``Claim`` desde el JSON del LLM (``{"text": ..., "source_id": ...}``)."""
        text = data.get("text")
        source_id = data.get("source_id")
        if not isinstance(text, str) or not isinstance(source_id, str):
            raise ValueError(f"claim mal formado: {dict(data)!r}")
        return cls(text=text, source_id=source_id)


@dataclass(frozen=True)
class GroundingReport:
    """
    Resultado de ``check_grounding``.

    - ``ok``: True si no hay ningún problema (el guion puede pasar a TTS).
    - ``problems``: mensajes legibles (en español) para el registro y para el
      reintento con prompt más estricto.
    - ``unsupported``: texto de cada dato sin respaldo (del guion o de un claim).
    - ``checked_facts``: todos los datos extraídos del guion, en orden de aparición.
    """
    ok: bool
    problems: tuple[str, ...]
    unsupported: tuple[str, ...]
    checked_facts: tuple[Fact, ...]
