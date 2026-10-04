"""
Extracción de datos verificables de un texto (§4.2 paso 3: "números, fechas y
nombres propios del guion deben aparecer en las fuentes").

Función pura y determinista, sin I/O. Orden de extracción (cada fragmento
reconocido se enmascara para que no se cuente dos veces):

1. **Fechas**: ISO (``1998-03-03``, ``1998-03``), ``3 de marzo de 1998``,
   ``marzo de 1998``, ``3 de marzo``, ``March 3, 1998``, ``3 March 1998``,
   ``March 1998`` y meses sueltos (``en marzo``). Se normalizan a ISO parcial.
2. **Números en cifras**: ``3``, ``10.000``, ``1,5``, ``3º``, ``3rd``. Un entero de
   4 cifras entre 1000 y 2099 es un año (``kind="year"``).
3. **Números en palabras** (``dos``, ``treinta y dos``, ``dos mil diez``,
   ``mil novecientos noventa y ocho``) y ordinales (``tercer``, ``segunda``).
   "un/una/uno" sueltos no cuentan (son artículos casi siempre), ni "primer/a".
4. **Nombres propios**: secuencias de palabras con mayúscula inicial (con enlaces
   tipo "de"/"del" dentro: "Ciudad de México"). Se quitan las palabras vacías
   iniciales ("El", "Desde"...). Una palabra suelta a inicio de frase cuenta como
   nombre propio salvo que sea una palabra vacía, aparezca en minúscula en otro
   punto del texto o tenga terminación de palabra común (``-mente``, ``-amos``...).
   Es más estricto que "ignorar el inicio de frase": un nombre inventado a inicio de
   frase ("Madrid la vio nacer") no se cuela. El precio son algunos falsos positivos,
   que llevan al reintento o a la versión sin dato (invariante 5).

Limitaciones conocidas (conservadoras: provocan rechazo, nunca aceptación falsa):
décadas en palabras ("los noventa" ≠ "1990s"), años en palabras en inglés,
transliteraciones de nombres ("Nueva York" ≠ "New York") salvo que la fuente traiga
ambas formas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from radio.grounding.lexicon import (
    ACCENTED_COMMON_ENDINGS,
    COMMON_SUFFIXES,
    NAME_CONNECTORS,
    SOLO_NOT_A_NUMBER,
    Lexicon,
    combined,
    fold,
)
from radio.grounding.models import Fact, FactKind

# Carácter con el que se enmascaran los fragmentos ya reconocidos: no es palabra,
# ni espacio, ni puntuación de fin de frase, así que corta secuencias.
_MASK = "\x00"

_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
_DIGITS_RE = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?:\.?[ºª°]|st|nd|rd|th|er)?")
_ORDINAL_SUFFIX_RE = re.compile(r"(?:\.?[ºª°]|st|nd|rd|th|er)$")
_ISO_RE = re.compile(r"(?<![\w-])(\d{4})-(\d{1,2})(?:-(\d{1,2}))?(?![\w-])")
_SENTENCE_END = set(".!?…\n")
_TRANSPARENT = set(" \t\r¿¡«»\"'“”‘’([—–-") | {_MASK}
_NUMBER_GAP_RE = re.compile(r"[ \t]+|[ \t]*-[ \t]*")
_NAME_GAP_RE = re.compile(r"[ \t]+|-")

MIN_YEAR, MAX_YEAR = 1000, 2099


@dataclass
class _Found:
    start: int
    fact: Fact


def canonical_number(value: Decimal | int) -> str:
    """Forma canónica de un número (``Decimal("2010")`` → ``"2010"``, ``1.50`` → ``"1.5"``)."""
    dec = Decimal(value).normalize()
    text = format(dec, "f")
    return text


def _date_norm(year: int | None, month: int | None, day: int | None) -> str | None:
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None
    if year is not None and not MIN_YEAR <= year <= MAX_YEAR:
        return None
    y = f"{year:04d}" if year is not None else "-"
    parts = [y]
    if month is not None:
        parts.append(f"{month:02d}")
    if day is not None:
        parts.append(f"{day:02d}")
    return "-".join(parts)


def parse_date_norm(norm: str) -> tuple[int | None, int | None, int | None]:
    """Inversa de la normalización de fechas: ``"--03-03"`` → ``(None, 3, 3)``."""
    if norm.startswith("--"):
        year = None
        rest = norm[2:].split("-")
    else:
        first, *rest = norm.split("-")
        year = int(first)
    month = int(rest[0]) if rest and rest[0] else None
    day = int(rest[1]) if len(rest) > 1 and rest[1] else None
    return year, month, day


class _Scanner:
    """Estado de una extracción: texto enmascarado + datos encontrados."""

    def __init__(self, text: str, lex: Lexicon, want_names: bool) -> None:
        self.original = text
        self.buf = list(text)
        self.lex = lex
        self.want_names = want_names
        self.found: list[_Found] = []

    @property
    def masked(self) -> str:
        return "".join(self.buf)

    def add(self, start: int, end: int, kind: FactKind, normalized: str) -> None:
        self.found.append(_Found(start, Fact(kind, self.original[start:end], normalized)))
        self.buf[start:end] = _MASK * (end - start)

    # ── Fechas ───────────────────────────────────────────────────────────────

    def _month_alt(self) -> str:
        names = sorted(self.lex.months, key=len, reverse=True)
        return "|".join(re.escape(n) for n in names) or r"(?!x)x"

    def _month(self, token: str) -> int | None:
        return self.lex.months.get(fold(token))

    def dates(self) -> None:
        for m in list(_ISO_RE.finditer(self.masked)):
            norm = _date_norm(int(m.group(1)), int(m.group(2)),
                              int(m.group(3)) if m.group(3) else None)
            if norm is not None:
                self.add(m.start(), m.end(), "date", norm)
        if not self.lex.months:
            return
        month = f"(?P<month>{self._month_alt()})"
        links = "|".join(re.escape(x) for x in self.lex.date_links) or r"(?!x)x"
        day = r"(?P<day>\d{1,2})(?:st|nd|rd|th|º)?"
        year = r"(?P<year>\d{4})"
        patterns = [
            # 3 de marzo de 1998 · 3 March 1998 · 3 de marzo
            rf"(?<![\w.,]){day}(?:\s+(?:{links}))?\s+{month}"
            rf"(?:,?\s+(?:(?:{links})\s+)?(?:año\s+)?{year})?(?![\w])",
            # March 3, 1998 · March 3
            rf"(?<![\w]){month}\s+{day}(?:,?\s+{year})?(?![\w.,]?\d)",
            # marzo de 1998 · March 1998
            rf"(?<![\w]){month},?\s+(?:(?:{links})\s+)?{year}(?!\w)",
            # mes suelto
            rf"(?<![\w]){month}(?!\w)",
        ]
        for pattern in patterns:
            for m in list(re.finditer(pattern, self.masked, re.IGNORECASE)):
                gd = m.groupdict()
                token = gd["month"]
                if (
                    gd.get("day") is None and gd.get("year") is None
                    and fold(token) in self.lex.capitalized_months
                    and not token[:1].isupper()
                ):
                    continue
                num = self._month(token)
                norm = _date_norm(
                    int(gd["year"]) if gd.get("year") else None,
                    num,
                    int(gd["day"]) if gd.get("day") else None,
                )
                if norm is not None:
                    self.add(m.start(), m.end(), "date", norm)

    # ── Números en cifras ────────────────────────────────────────────────────

    def digits(self) -> None:
        for m in list(_DIGITS_RE.finditer(self.masked)):
            raw = _ORDINAL_SUFFIX_RE.sub("", m.group(0))
            value = _parse_digits(raw)
            if value is None:
                continue
            is_year = (
                raw.isdigit() and len(raw) == 4 and MIN_YEAR <= int(raw) <= MAX_YEAR
                and raw == m.group(0)
            )
            self.add(m.start(), m.end(), "year" if is_year else "number",
                     canonical_number(value))

    # ── Números en palabras ──────────────────────────────────────────────────

    def number_words(self) -> None:
        text = self.masked
        words = list(_WORD_RE.finditer(text))
        lex = self.lex
        spans: list[tuple[int, int, int, bool]] = []   # start, end, value, es_año
        i = 0
        while i < len(words):
            w = fold(words[i].group())
            if w in lex.ordinals:
                spans.append((words[i].start(), words[i].end(), lex.ordinals[w], False))
                i += 1
                continue
            if w not in lex.cardinals and w not in lex.multipliers:
                i += 1
                continue
            parsed = _parse_number_run(words, i, text, lex)
            if parsed is None:
                i += 1
                continue
            end_idx, value, has_thousand = parsed
            only = fold(words[i].group())
            if not (end_idx == i and only in SOLO_NOT_A_NUMBER):
                is_year = has_thousand and 1100 <= value <= MAX_YEAR
                spans.append((words[i].start(), words[end_idx].end(), value, is_year))
            i = end_idx + 1
        for start, end, value, is_year in spans:
            self.add(start, end, "year" if is_year else "number", canonical_number(value))

    # ── Nombres propios ──────────────────────────────────────────────────────

    def names(self) -> None:
        text = self.masked
        words = list(_WORD_RE.finditer(text))
        lowercase_seen = {fold(w.group()) for w in words if w.group()[:1].islower()}
        i = 0
        while i < len(words):
            if not words[i].group()[:1].isupper():
                i += 1
                continue
            seq = [i]
            j = i + 1
            while j < len(words) and _adjacent(words[j - 1], words[j], text, _NAME_GAP_RE):
                token = words[j].group()
                if token[:1].isupper():
                    seq.append(j)
                    j += 1
                    continue
                # enlace dentro del nombre ("Ciudad de México") si sigue mayúscula
                if (
                    fold(token) in NAME_CONNECTORS and j + 1 < len(words)
                    and words[j + 1].group()[:1].isupper()
                    and _adjacent(words[j], words[j + 1], text, _NAME_GAP_RE)
                ):
                    seq.extend([j, j + 1])
                    j += 2
                    continue
                break
            self._emit_name(words, seq, text, lowercase_seen)
            i = j

    def _emit_name(
        self,
        words: list[re.Match[str]],
        seq: list[int],
        text: str,
        lowercase_seen: set[str],
    ) -> None:
        first_idx = seq[0]
        drop = self.lex.stopwords | NAME_CONNECTORS

        def strip(items: list[int]) -> list[int]:
            while items and fold(words[items[0]].group()) in drop:
                items = items[1:]
            while items and fold(words[items[-1]].group()) in NAME_CONNECTORS:
                items = items[:-1]
            return items

        seq = strip(seq)
        # palabra común con mayúscula solo por ir a inicio de frase ("Escuchad Nube Ferrán")
        if seq and seq[0] == first_idx and _sentence_initial(text, words[first_idx].start()):
            if _looks_common(words[first_idx].group(), lowercase_seen):
                seq = strip(seq[1:])
        if not seq:
            return
        start, end = words[seq[0]].start(), words[seq[-1]].end()
        normalized = " ".join(fold(words[k].group()) for k in seq)
        self.add(start, end, "proper_noun", normalized)

    def run(self) -> list[Fact]:
        self.dates()
        self.digits()
        self.number_words()
        if self.want_names:
            self.names()
        return [f.fact for f in sorted(self.found, key=lambda f: f.start)]


def _parse_digits(raw: str) -> Decimal | None:
    """``"10.000"`` → 10000, ``"1,5"`` → 1.5, ``"2.500.000"`` → 2500000."""
    groups = re.split(r"[.,]", raw)
    try:
        if len(groups) == 1:
            return Decimal(raw)
        if all(len(g) == 3 for g in groups[1:]) and len(groups[0]) <= 3:
            return Decimal("".join(groups))       # separador de miles
        if len(groups) == 2:
            return Decimal(f"{groups[0]}.{groups[1]}")   # decimal
    except InvalidOperation:
        return None
    return None


def _looks_common(word: str, lowercase_seen: set[str]) -> bool:
    """Palabra con mayúscula a inicio de frase que parece común, no nombre propio."""
    w = fold(word)
    return (
        w in lowercase_seen
        or w.endswith(COMMON_SUFFIXES)
        or word.lower().endswith(ACCENTED_COMMON_ENDINGS)
    )


def _adjacent(a: re.Match[str], b: re.Match[str], text: str, gap: re.Pattern[str]) -> bool:
    return gap.fullmatch(text[a.end():b.start()]) is not None


def _sentence_initial(text: str, pos: int) -> bool:
    k = pos - 1
    while k >= 0:
        ch = text[k]
        if ch in _SENTENCE_END:
            return True
        if ch not in _TRANSPARENT:
            return False
        k -= 1
    return True


def _can_follow(last_add: int | None, value: int) -> bool:
    """¿Puede ``value`` sumarse tras el último sumando ``last_add`` en la misma cifra?"""
    if last_add is None:
        return True
    if last_add >= 100 and last_add % 100 == 0:
        return value < 100
    if 20 <= last_add < 100 and last_add % 10 == 0:
        return value < 10
    return False


def _parse_number_run(
    words: list[re.Match[str]], i: int, text: str, lex: Lexicon
) -> tuple[int, int, bool] | None:
    """
    Lee una expresión numérica en palabras que empieza en ``words[i]``.
    Devuelve (índice de la última palabra, valor, contiene "mil"/"thousand").
    """
    total = 0
    current = 0
    last_add: int | None = None
    seen_thousand = seen_million = False
    has_thousand = False
    last_used = -1
    k = i
    while k < len(words):
        if k > i and not _adjacent(words[k - 1], words[k], text, _NUMBER_GAP_RE):
            break
        w = fold(words[k].group())
        if w in lex.connectors and k > i:
            if (
                k + 1 < len(words)
                and _adjacent(words[k], words[k + 1], text, _NUMBER_GAP_RE)
                and fold(words[k + 1].group()) in lex.cardinals
                and _can_follow(last_add, lex.cardinals[fold(words[k + 1].group())])
                and last_add is not None
            ):
                k += 1
                continue
            break
        if w in lex.cardinals:
            value = lex.cardinals[w]
            if k > i and not _can_follow(last_add, value):
                break
            current += value
            last_add = value
        elif w in lex.multipliers:
            mult = lex.multipliers[w]
            if mult == 100:
                if not 1 <= current <= 9:
                    break
                current *= 100
                last_add = current
            elif mult == 1000:
                if seen_thousand:
                    break
                total += max(current, 1) * 1000
                current, last_add = 0, None
                seen_thousand = has_thousand = True
            else:
                if seen_million:
                    break
                total = max(total + current, 1) * mult
                current, last_add = 0, None
                seen_million, seen_thousand = True, False
        else:
            break
        last_used = k
        k += 1
    if last_used < i:
        return None
    return last_used, total + current, has_thousand


def _extract(text: str, langs: tuple[str, ...], *, want_names: bool = True) -> list[Fact]:
    return _Scanner(text, combined(langs), want_names).run()


def extract_facts(text: str, lang: str = "es") -> list[Fact]:
    """
    Datos verificables de ``text`` (números, años, fechas y nombres propios), en
    orden de aparición. ``lang`` elige el léxico ("es", "en"; otro idioma sin tablas
    se procesa solo con cifras, fechas ISO y nombres propios).
    """
    return _extract(text, (lang,))
