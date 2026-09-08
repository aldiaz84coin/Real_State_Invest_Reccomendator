"""Portal de Subastas del BOE: inmuebles y fincas en subasta publica.

Es la unica fuente gratuita y legal de ofertas activas de inmuebles en toda
Espana. A diferencia de los portales privados, se trata de informacion del
sector publico sujeta al regimen de reutilizacion, y encaja con el proposito de
la aplicacion: en subasta el suelo suele salir por debajo de mercado.

Cada subasta publica valor de tasacion, puja minima, tipo de bien y, muy a
menudo, referencia catastral, que permite cruzarla con la geometria real de la
parcela sin depender de que el anuncio traiga coordenadas.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from app.sources.base import BaseSource, SourceError, SourceStatus

# Codigos de provincia del INE, que es lo que usa el portal en sus filtros.
PROVINCE_CODES: dict[str, str] = {
    "cantabria": "39", "asturias": "33", "malaga": "29", "granada": "18",
    "almeria": "04", "cadiz": "11", "murcia": "30", "alicante": "03",
    "valencia": "46", "castellon": "12", "tarragona": "43", "girona": "17",
    "barcelona": "08", "baleares": "07", "las palmas": "35",
    "santa cruz de tenerife": "38", "pontevedra": "36", "a coruna": "15",
    "lugo": "27", "guipuzcoa": "20", "vizcaya": "48", "huelva": "21",
}

# Etiquetas del detalle de subasta que interesan, normalizadas sin acentos.
FIELD_LABELS = {
    "valor subasta": "auction_value",
    "tasacion": "appraisal_value",
    "valor de tasacion": "appraisal_value",
    "puja minima": "minimum_bid",
    "importe del deposito": "deposit",
    "tramos entre pujas": "bid_step",
    "cantidad reclamada": "claimed_amount",
    "descripcion": "description",
    "direccion": "address",
    "codigo postal": "postal_code",
    "localidad": "municipality",
    "provincia": "province",
    "referencia catastral": "cadastral_ref",
    "tipo de bien": "asset_type",
    "situacion posesoria": "possession",
    "cargas": "charges",
    "fecha de conclusion": "end_date",
    "fecha de inicio": "start_date",
    "superficie": "area",
}

# Tipos de bien que interesan: suelo, no pisos ni garajes.
LAND_KEYWORDS = ("finca rustica", "solar", "terreno", "suelo", "parcela",
                 "finca urbana sin edificar")


class _TableParser(HTMLParser):
    """Extrae pares etiqueta/valor de las tablas del detalle de subasta.

    El portal presenta cada dato como una fila con encabezado y celda. Leer los
    pares en vez de posiciones fijas hace el parseo inmune a que anadan o
    reordenen filas, que es lo que suele romper estos raspados.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self._current_label: str | None = None
        self._buffer: list[str] = []
        self._in_header = False
        self._in_cell = False
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "th":
            self._flush_cell()
            self._in_header, self._buffer = True, []
        elif tag == "td":
            self._in_cell, self._buffer = True, []
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._link_href, self._link_text = href, []

    def handle_endtag(self, tag: str) -> None:
        if tag == "th" and self._in_header:
            self._current_label = _normalize(" ".join(self._buffer))
            self._in_header, self._buffer = False, []
        elif tag == "td" and self._in_cell:
            self._flush_cell()
        elif tag == "a" and self._link_href is not None:
            text = " ".join(self._link_text).strip()
            self.links.append((self._link_href, text))
            self._link_href, self._link_text = None, []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        if self._in_header or self._in_cell:
            self._buffer.append(stripped)
        if self._link_href is not None:
            self._link_text.append(stripped)

    def _flush_cell(self) -> None:
        if self._in_cell and self._current_label:
            value = " ".join(self._buffer).strip()
            if value and self._current_label not in self.pairs:
                self.pairs[self._current_label] = value
        self._in_cell, self._buffer = False, []


class BoeSubastasSource(BaseSource):
    key = "boe_subastas"
    name = "Subastas del BOE (inmuebles)"
    kind = "listings"
    required = False
    docs_url = "https://subastas.boe.es/"
    licence = (
        "Información del sector público. Reutilización permitida conforme a la "
        "Ley 37/2007; se cita la fuente y no se altera el contenido."
    )

    @property
    def base_url(self) -> str:
        return "https://subastas.boe.es"

    def search(
        self,
        province: str | None = None,
        *,
        only_land: bool = True,
        max_results: int = 40,
    ) -> list[str]:
        """Devuelve los identificadores de subasta que encajan con el filtro."""
        params: dict[str, Any] = {
            "accion": "Buscar",
            "campo[0]": "SUBASTA.ESTADO",
            "dato[0]": "EJ",             # en ejecución: sólo subastas vivas
            "campo[1]": "BIEN.TIPO",
            "dato[1]": "I",              # inmuebles
            "sort_field[0]": "SUBASTA.FECHA_FIN_YMD",
            "sort_order[0]": "desc",
            "page_hits": min(max_results, 50),
        }
        code = self.province_code(province)
        if code:
            params["campo[2]"] = "BIEN.PROVINCIA"
            params["dato[2]"] = code

        response = self.request("GET", f"{self.base_url}/subastas_ava.php", params=params)
        if response.status_code != 200:
            raise SourceError(f"Subastas del BOE: HTTP {response.status_code}")
        return self.parse_result_ids(response.text)[:max_results]

    @staticmethod
    def province_code(province: str | None) -> str | None:
        if not province:
            return None
        if province.isdigit():
            return province.zfill(2)
        return PROVINCE_CODES.get(_normalize(province))

    @staticmethod
    def parse_result_ids(html: str) -> list[str]:
        """Identificadores de subasta presentes en una página de resultados."""
        # Se buscan por patrón y no por estructura: el listado cambia de
        # maquetación con frecuencia, pero el identificador tiene forma fija.
        found = re.findall(r"idSub=([A-Z0-9\-]+)", html)
        unique: list[str] = []
        for identifier in found:
            if identifier not in unique:
                unique.append(identifier)
        return unique

    def detail(self, id_sub: str) -> dict[str, Any]:
        """Datos de una subasta concreta, ya normalizados."""
        response = self.request(
            "GET", f"{self.base_url}/detalleSubasta.php", params={"idSub": id_sub}
        )
        if response.status_code != 200:
            raise SourceError(f"Subastas del BOE: HTTP {response.status_code} en {id_sub}")
        return self.parse_detail(response.text, id_sub)

    @classmethod
    def parse_detail(cls, html: str, id_sub: str) -> dict[str, Any]:
        parser = _TableParser()
        parser.feed(html)

        fields: dict[str, str] = {}
        for label, value in parser.pairs.items():
            key = FIELD_LABELS.get(label)
            if key and key not in fields:
                fields[key] = value

        return {
            "id_sub": id_sub,
            "url": f"https://subastas.boe.es/detalleSubasta.php?idSub={id_sub}",
            "raw_fields": parser.pairs,
            **fields,
        }

    @staticmethod
    def is_land(detail: dict[str, Any]) -> bool:
        """¿La subasta es de suelo y no de un piso o un garaje?"""
        haystack = _normalize(
            " ".join(str(detail.get(k, "")) for k in ("asset_type", "description"))
        )
        return any(keyword in haystack for keyword in LAND_KEYWORDS)

    @classmethod
    def normalize(cls, detail: dict[str, Any]) -> dict[str, Any] | None:
        """Traduce una subasta al esquema interno de anuncio.

        El precio de referencia es la puja mínima si existe, y si no el valor
        de subasta: es lo que de verdad hay que pagar, no la tasación.
        """
        price = (
            _parse_amount(detail.get("minimum_bid"))
            or _parse_amount(detail.get("auction_value"))
            or _parse_amount(detail.get("appraisal_value"))
        )
        area = _parse_area(detail.get("area")) or _parse_area(detail.get("description"))
        if not price or not area:
            return None

        return {
            "source": "boe_subastas",
            "external_id": detail["id_sub"],
            "url": detail["url"],
            "title": (detail.get("asset_type") or "Subasta de inmueble")[:200],
            "description": (detail.get("description") or "")[:4000],
            "price_eur": price,
            "area_m2": area,
            "price_eur_m2": round(price / area, 2),
            "lat": None,
            "lon": None,
            "coords_precision": "missing",
            "address": detail.get("address", ""),
            "municipality_name": detail.get("municipality", ""),
            "province": detail.get("province", ""),
            "land_type": detail.get("asset_type", "subasta"),
            "cadastral_ref": _clean_cadastral(detail.get("cadastral_ref")),
            "raw": detail,
        }

    def check(self) -> SourceStatus:
        return self._timed_probe(f"{self.base_url}/subastas_ava.php")


def _normalize(text: str) -> str:
    """Minúsculas, sin acentos y sin puntuación final, para comparar etiquetas."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\s:]+", " ", without_accents).strip(" :.")


def _parse_amount(value: Any) -> float | None:
    """Importes del BOE: '12.345,67 €'."""
    if value is None:
        return None
    match = re.search(r"[\d.,]+", str(value))
    if not match:
        return None
    text = match.group(0).replace(".", "").replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        return None
    return amount if amount > 0 else None


def _parse_area(value: Any) -> float | None:
    """Superficie en metros cuadrados, incluso embebida en la descripción.

    Acepta hectáreas y áreas porque las fincas rústicas se describen así, y
    convertirlas mal cambiaría el precio por metro en dos órdenes de magnitud.
    """
    if value is None:
        return None
    text = _normalize(str(value))

    hectares = re.search(r"([\d.,]+)\s*(?:hectareas?|has?\b)", text)
    if hectares:
        amount = _parse_amount(hectares.group(1))
        return amount * 10_000 if amount else None

    areas = re.search(r"([\d.,]+)\s*areas?\b", text)
    if areas:
        amount = _parse_amount(areas.group(1))
        return amount * 100 if amount else None

    metros = re.search(r"([\d.,]+)\s*(?:m2|m²|metros cuadrados)", text)
    if metros:
        return _parse_amount(metros.group(1))
    return None


def _clean_cadastral(value: Any) -> str | None:
    """La referencia catastral son 20 caracteres alfanuméricos."""
    if not value:
        return None
    match = re.search(r"[0-9A-Z]{20}", str(value).upper().replace(" ", ""))
    return match.group(0) if match else None
