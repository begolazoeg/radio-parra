"""
Léxico del extractor de datos: números en palabras, ordinales, meses y palabras
vacías, por idioma (``"es"``, ``"en"``).

Es deliberadamente pequeño y explícito: cubre lo habitual en una intro de radio,
no pretende ser un parser de lenguaje natural. Para añadir un idioma (p. ej. ``"ca"``)
basta con añadir sus tablas a ``LEXICONS``; un idioma sin tablas se trata solo con
cifras, fechas ISO y nombres propios.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


def fold(text: str) -> str:
    """Minúsculas sin tildes ni diacríticos (``"Bogotá"`` → ``"bogota"``)."""
    decomposed = unicodedata.normalize("NFKD", text.replace("’", "'"))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


@dataclass(frozen=True)
class Lexicon:
    """Tablas de un idioma. Todas las claves van ya plegadas con ``fold``."""
    cardinals: dict[str, int] = field(default_factory=dict)
    multipliers: dict[str, int] = field(default_factory=dict)   # 100, 1000, 10**6
    ordinals: dict[str, int] = field(default_factory=dict)
    connectors: frozenset[str] = frozenset()                    # "y" / "and"
    months: dict[str, int] = field(default_factory=dict)
    # Meses que solo cuentan solos si van con mayúscula (en: "May" sí, "may" no)
    capitalized_months: frozenset[str] = frozenset()
    date_links: tuple[str, ...] = ()    # "de", "del", "of"
    stopwords: frozenset[str] = frozenset()


# ── Español ───────────────────────────────────────────────────────────────────

_ES_CARDINALS = {
    "cero": 0, "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21,
    "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
    "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
    "setenta": 70, "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100,
    "doscientos": 200, "doscientas": 200, "trescientos": 300, "trescientas": 300,
    "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500, "quinientas": 500,
    "seiscientos": 600, "seiscientas": 600, "setecientos": 700, "setecientas": 700,
    "ochocientos": 800, "ochocientas": 800, "novecientos": 900, "novecientas": 900,
}
_ES_MULTIPLIERS = {"mil": 1000, "millon": 10**6, "millones": 10**6}
# "primer/primera" se excluye a propósito: "por primera vez" no es un dato.
_ES_ORDINALS = {
    "segundo": 2, "segunda": 2, "tercer": 3, "tercero": 3, "tercera": 3,
    "cuarto": 4, "cuarta": 4, "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6,
    "septimo": 7, "septima": 7, "octavo": 8, "octava": 8, "noveno": 9, "novena": 9,
    "decimo": 10, "decima": 10,
}
_ES_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_ES_STOPWORDS = frozenset(fold(w) for w in """
    a al ante bajo con contra de del desde durante en entre hacia hasta mediante para por
    segun sin so sobre tras versus via
    el la lo los las un una unos unas
    y e o u ni pero mas sino aunque porque pues que si no
    yo tu el ella ello nosotros nosotras vosotros vosotras ellos ellas usted ustedes
    me te se nos os le les mi mis tu tus su sus nuestro nuestra nuestros nuestras
    este esta esto estos estas ese esa eso esos esas aquel aquella aquello aquellos aquellas
    cada otro otra otros otras todo toda todos todas algo alguien nada nadie mucho mucha
    muchos muchas poco poca pocos pocas tanto tanta
    que quien quienes cual cuales como cuando donde cuanto cuanta
    hoy ahora ayer manana aqui ahi alli alla ya aun todavia siempre nunca jamas tambien
    tampoco ademas despues antes luego entonces asi bien mal muy mas menos solo solamente
    incluso quiza quizas apenas casi precisamente justo claro
    lunes martes miercoles jueves viernes sabado domingo
    hola buenas buenos bienvenidos bienvenidas gracias adios atencion ojo venga vale
    es son era eran fue fueron sera seran ha han habia hay hubo tiene tienen tenia
    esta estan estaba estamos somos soy eres
    nacio nacida nacido crecio vive vivio llega llegan llego viene vienen vino
    suena sonando escuchamos escuchemos escuchen escucha oimos vamos seguimos
    continuamos empezamos arrancamos presentamos dejamos disfruten disfrutad
    formada formado originaria originario conocida conocido
    musica cancion canciones concierto conciertos disco discos album albumes tema temas
    grupo banda cantante voz voces artista artistas sesion
    atentos atentas listos listas bueno buena feliz felices gran grande nuevo nueva nuevos
    nuevas ultimo ultima pronto enseguida dentro fuera cerca lejos juntos juntas directo
    escuchen oigan miren mira mirad cuidado silencio sonido ritmo guitarra piano bateria
    cumbia folk rock pop jazz salsa tango flamenco bolero rap soul funk blues reggae
    reggaeton electronica
""".split())

ES = Lexicon(
    cardinals=_ES_CARDINALS,
    multipliers=_ES_MULTIPLIERS,
    ordinals=_ES_ORDINALS,
    connectors=frozenset({"y"}),
    months=_ES_MONTHS,
    date_links=("de", "del"),
    stopwords=_ES_STOPWORDS,
)

# ── Inglés (para fuentes y claims en inglés) ─────────────────────────────────

_EN_CARDINALS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_EN_MULTIPLIERS = {"hundred": 100, "thousand": 1000, "million": 10**6, "millions": 10**6}
_EN_ORDINALS = {
    "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_EN_STOPWORDS = frozenset("""
    the a an and or but nor of in on at by for with from to into onto as after before
    during since until about over under between through
    is was are were be been being has have had do does did will would can could
    he she it they we you i his her its their our your my this that these those
    there here then when where while who whom which what why how not no yes also
    born she her
    monday tuesday wednesday thursday friday saturday sunday
""".split())

EN = Lexicon(
    cardinals=_EN_CARDINALS,
    multipliers=_EN_MULTIPLIERS,
    ordinals=_EN_ORDINALS,
    connectors=frozenset({"and"}),
    months=_EN_MONTHS,
    capitalized_months=frozenset(_EN_MONTHS),
    date_links=("of",),
    stopwords=_EN_STOPWORDS,
)

LEXICONS: dict[str, Lexicon] = {"es": ES, "en": EN}

# Palabras con significado numérico en un idioma y común en otro ("once" = 11 en
# español, "una vez" en inglés). Al combinar idiomas (índice de fuentes) se descartan.
AMBIGUOUS_ACROSS_LANGS = frozenset({"once"})

# Palabras que solas no son un dato ("un disco", "one of the"): solo cuentan
# dentro de una expresión mayor ("un millón", "veintiuno").
SOLO_NOT_A_NUMBER = frozenset({"un", "una", "uno", "one"})

# Enlaces dentro de un nombre propio compuesto ("Ciudad de México", "Van der Berg")
NAME_CONNECTORS = frozenset({"de", "del", "la", "las", "los", "da", "das", "do", "dos",
                             "di", "van", "von", "der", "den", "du", "le"})

# Terminaciones de palabra común: una palabra con mayúscula a inicio de frase que
# acaba así se trata como palabra común, no como nombre propio.
COMMON_SUFFIXES = ("mente", "ando", "iendo", "yendo", "amos", "emos", "imos",
                   "aron", "ieron", "aban", "abamos", "nse")
# Igual, pero sobre la palabra con tildes: pretérito en "-ó" ("Vendió", "Grabó")
ACCENTED_COMMON_ENDINGS = ("ó",)


def combined(langs: tuple[str, ...]) -> Lexicon:
    """
    Fusiona las tablas de varios idiomas. Si hay más de uno, se quitan las palabras
    ambiguas entre idiomas (``AMBIGUOUS_ACROSS_LANGS``). Idiomas sin tablas se ignoran
    (solo aportan cifras, fechas ISO y nombres propios).
    """
    known = [LEXICONS[lang] for lang in langs if lang in LEXICONS]
    if len(known) == 1:
        return known[0]
    drop = AMBIGUOUS_ACROSS_LANGS if len(known) > 1 else frozenset()

    def merge(attr: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for lex in known:
            out.update(getattr(lex, attr))
        return {k: v for k, v in out.items() if k not in drop}

    def union(attr: str) -> frozenset[str]:
        out: frozenset[str] = frozenset()
        for lex in known:
            out |= getattr(lex, attr)
        return out

    return Lexicon(
        cardinals=merge("cardinals"),
        multipliers=merge("multipliers"),
        ordinals=merge("ordinals"),
        connectors=union("connectors"),
        months=merge("months"),
        capitalized_months=union("capitalized_months"),
        date_links=tuple(dict.fromkeys(link for lex in known for link in lex.date_links)),
        stopwords=union("stopwords") if known else ES.stopwords | EN.stopwords,
    )
