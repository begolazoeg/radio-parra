"""
Nombres de país bilingües (español / inglés) para códigos ISO 3166-1 alfa-2.

MusicBrainz da el país como código (``"US"``) y las áreas en inglés. El guion va en
español, y el grounding compara nombres propios literalmente (sin traducir), así
que la ficha de MusicBrainz lleva las dos formas y alias comunes ("EE. UU.").
Solo países frecuentes; un código desconocido se deja tal cual.
"""

from __future__ import annotations

# código → (español, inglés)
COUNTRIES: dict[str, tuple[str, str]] = {
    "AR": ("Argentina", "Argentina"),
    "AT": ("Austria", "Austria"),
    "AU": ("Australia", "Australia"),
    "BE": ("Bélgica", "Belgium"),
    "BO": ("Bolivia", "Bolivia"),
    "BR": ("Brasil", "Brazil"),
    "CA": ("Canadá", "Canada"),
    "CH": ("Suiza", "Switzerland"),
    "CL": ("Chile", "Chile"),
    "CM": ("Camerún", "Cameroon"),
    "CN": ("China", "China"),
    "CO": ("Colombia", "Colombia"),
    "CR": ("Costa Rica", "Costa Rica"),
    "CU": ("Cuba", "Cuba"),
    "CV": ("Cabo Verde", "Cape Verde"),
    "CZ": ("Chequia", "Czech Republic"),
    "DE": ("Alemania", "Germany"),
    "DK": ("Dinamarca", "Denmark"),
    "DO": ("República Dominicana", "Dominican Republic"),
    "DZ": ("Argelia", "Algeria"),
    "EC": ("Ecuador", "Ecuador"),
    "EG": ("Egipto", "Egypt"),
    "ES": ("España", "Spain"),
    "ET": ("Etiopía", "Ethiopia"),
    "FI": ("Finlandia", "Finland"),
    "FR": ("Francia", "France"),
    "GB": ("Reino Unido", "United Kingdom"),
    "GH": ("Ghana", "Ghana"),
    "GR": ("Grecia", "Greece"),
    "GT": ("Guatemala", "Guatemala"),
    "HN": ("Honduras", "Honduras"),
    "HT": ("Haití", "Haiti"),
    "HU": ("Hungría", "Hungary"),
    "ID": ("Indonesia", "Indonesia"),
    "IE": ("Irlanda", "Ireland"),
    "IL": ("Israel", "Israel"),
    "IN": ("India", "India"),
    "IS": ("Islandia", "Iceland"),
    "IT": ("Italia", "Italy"),
    "JM": ("Jamaica", "Jamaica"),
    "JP": ("Japón", "Japan"),
    "KE": ("Kenia", "Kenya"),
    "KR": ("Corea del Sur", "South Korea"),
    "MA": ("Marruecos", "Morocco"),
    "ML": ("Malí", "Mali"),
    "MX": ("México", "Mexico"),
    "NG": ("Nigeria", "Nigeria"),
    "NI": ("Nicaragua", "Nicaragua"),
    "NL": ("Países Bajos", "Netherlands"),
    "NO": ("Noruega", "Norway"),
    "NZ": ("Nueva Zelanda", "New Zealand"),
    "PA": ("Panamá", "Panama"),
    "PE": ("Perú", "Peru"),
    "PH": ("Filipinas", "Philippines"),
    "PL": ("Polonia", "Poland"),
    "PR": ("Puerto Rico", "Puerto Rico"),
    "PT": ("Portugal", "Portugal"),
    "PY": ("Paraguay", "Paraguay"),
    "RO": ("Rumanía", "Romania"),
    "RU": ("Rusia", "Russia"),
    "SE": ("Suecia", "Sweden"),
    "SN": ("Senegal", "Senegal"),
    "SV": ("El Salvador", "El Salvador"),
    "TR": ("Turquía", "Turkey"),
    "TT": ("Trinidad y Tobago", "Trinidad and Tobago"),
    "UA": ("Ucrania", "Ukraine"),
    "US": ("Estados Unidos", "United States"),
    "UY": ("Uruguay", "Uruguay"),
    "VE": ("Venezuela", "Venezuela"),
    "ZA": ("Sudáfrica", "South Africa"),
}

# Alias habituales en guiones y fuentes
ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("EE. UU.", "EEUU", "USA"),
    "GB": ("Gran Bretaña", "Great Britain", "UK"),
    "NL": ("Holanda", "Holland"),
}


def country_names(code: str) -> tuple[str, ...]:
    """
    Formas del país ``code`` (español primero, luego inglés, alias y el código),
    sin repetir. ``("Estados Unidos", "United States", "EE. UU.", ..., "US")``.
    """
    code = code.upper()
    names: list[str] = list(COUNTRIES.get(code, ()))
    names.extend(ALIASES.get(code, ()))
    names.append(code)
    return tuple(dict.fromkeys(names))


def spanish_name_for(english: str) -> str | None:
    """Nombre en español de un país dado en inglés (``"Mexico"`` → ``"México"``)."""
    for es, en in COUNTRIES.values():
        if en.casefold() == english.casefold():
            return es
    return None
