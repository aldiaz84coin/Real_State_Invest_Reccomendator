"""Provincias españolas con su código del INE.

Vive aparte porque la necesitan tres sitios a la vez: el selector del
buscador, el filtro de las Subastas del BOE (que busca por código, no por
nombre) y la normalización de lo que devuelven las fuentes. El conector del
BOE llevaba sólo las veintidós provincias de costa y montaña, de modo que
buscar en Toledo o en Zamora se hacía sin filtro y traía otra cosa.
"""
from __future__ import annotations

import unicodedata

# Código del INE → nombre oficial. El código es el que usa el portal de
# subastas en sus formularios, y coincide con los dos primeros dígitos del
# código de municipio, así que sirve también para cruzar con el INE.
PROVINCES: dict[str, str] = {
    "01": "Araba/Álava",
    "02": "Albacete",
    "03": "Alicante/Alacant",
    "04": "Almería",
    "05": "Ávila",
    "06": "Badajoz",
    "07": "Illes Balears",
    "08": "Barcelona",
    "09": "Burgos",
    "10": "Cáceres",
    "11": "Cádiz",
    "12": "Castellón/Castelló",
    "13": "Ciudad Real",
    "14": "Córdoba",
    "15": "A Coruña",
    "16": "Cuenca",
    "17": "Girona",
    "18": "Granada",
    "19": "Guadalajara",
    "20": "Gipuzkoa",
    "21": "Huelva",
    "22": "Huesca",
    "23": "Jaén",
    "24": "León",
    "25": "Lleida",
    "26": "La Rioja",
    "27": "Lugo",
    "28": "Madrid",
    "29": "Málaga",
    "30": "Murcia",
    "31": "Navarra",
    "32": "Ourense",
    "33": "Asturias",
    "34": "Palencia",
    "35": "Las Palmas",
    "36": "Pontevedra",
    "37": "Salamanca",
    "38": "Santa Cruz de Tenerife",
    "39": "Cantabria",
    "40": "Segovia",
    "41": "Sevilla",
    "42": "Soria",
    "43": "Tarragona",
    "44": "Teruel",
    "45": "Toledo",
    "46": "Valencia/València",
    "47": "Valladolid",
    "48": "Bizkaia",
    "49": "Zamora",
    "50": "Zaragoza",
    "51": "Ceuta",
    "52": "Melilla",
}

# Grafías alternativas que llegan de las fuentes o que escribe el usuario. Un
# anuncio dice «Baleares» y el INE «Illes Balears»; sin esto la provincia no
# se reconocía y la búsqueda salía sin filtro.
ALIASES: dict[str, str] = {
    "alava": "01", "araba": "01",
    "alicante": "03", "alacant": "03",
    "baleares": "07", "islas baleares": "07", "illes balears": "07",
    "mallorca": "07", "menorca": "07", "ibiza": "07", "eivissa": "07",
    "castellon": "12", "castello": "12",
    "coruna": "15", "la coruna": "15", "a coruna": "15",
    "guipuzcoa": "20", "gipuzkoa": "20",
    "gerona": "17", "girona": "17",
    "lerida": "25", "lleida": "25",
    "orense": "32", "ourense": "32",
    "rioja": "26", "la rioja": "26", "logrono": "26",
    "santa cruz de tenerife": "38", "tenerife": "38",
    "las palmas": "35", "gran canaria": "35",
    "valencia": "46", "valencia/valencia": "46",
    "vizcaya": "48", "bizkaia": "48", "bilbao": "48",
    "asturias": "33", "principado de asturias": "33", "oviedo": "33",
    "navarra": "31", "nafarroa": "31", "pamplona": "31",
    "madrid": "28", "murcia": "30", "cantabria": "39", "santander": "39",
}


def normalize(text: str) -> str:
    """Minúsculas y sin acentos: es como se comparan los nombres aquí."""
    limpio = unicodedata.normalize("NFKD", text or "")
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    return limpio.strip().lower()


def code_for(province: str | None) -> str | None:
    """Código INE de una provincia escrita de cualquier forma razonable."""
    if not province:
        return None
    texto = province.strip()
    if texto.isdigit():
        codigo = texto.zfill(2)
        return codigo if codigo in PROVINCES else None

    clave = normalize(texto)
    if clave in ALIASES:
        return ALIASES[clave]
    for codigo, nombre in PROVINCES.items():
        if normalize(nombre) == clave:
            return codigo
    # «Valencia/València» y compañía: basta con acertar una de las dos formas.
    for codigo, nombre in PROVINCES.items():
        if clave in [normalize(parte) for parte in nombre.split("/")]:
            return codigo
    return None


def name_for(code: str | None) -> str | None:
    return PROVINCES.get((code or "").zfill(2)) if code else None


def names() -> list[str]:
    """Los cincuenta y dos nombres, en orden alfabético, para el selector."""
    return sorted(PROVINCES.values(), key=normalize)


def spellings(province: str | None) -> list[str]:
    """Todas las formas en que puede venir escrita una provincia.

    Las fuentes no usan el nombre del INE: una subasta dice «Baleares» y el
    selector «Illes Balears». Filtrar por el texto exacto dejaba fuera anuncios
    que sí eran de esa provincia.
    """
    codigo = code_for(province)
    if codigo is None:
        return [province.strip()] if province else []

    formas = {PROVINCES[codigo]}
    formas.update(PROVINCES[codigo].split("/"))
    formas.update(alias for alias, destino in ALIASES.items() if destino == codigo)
    # Las de una sola letra o dos sobran, y los alias de capital (Bilbao,
    # Santander) no son nombres de provincia aunque sirvan para reconocerla.
    return sorted({f.strip() for f in formas if len(f.strip()) > 2})
