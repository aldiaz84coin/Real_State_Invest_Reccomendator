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

from app.provinces import code_for
from app.sources.base import BaseSource, SourceError, SourceStatus

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
    radius_note = (
        "No usa radio: filtra por provincia, o busca en toda España si no se "
        "indica ninguna."
    )
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

    def search_strategies(
        self, province: str | None, max_results: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """Juegos de parametros a probar, del mas especifico al mas simple.

        La forma exacta de la busqueda del portal no esta documentada y sus
        formularios han cambiado con los anos, asi que en vez de fijar una sola
        se prueban varias y se usa la primera que devuelva subastas. La ultima
        es deliberadamente minima: si ninguna funciona, al menos dice si el
        portal responde.
        """
        code = self.province_code(province)
        estrategias: list[tuple[str, dict[str, Any]]] = []

        base: dict[str, Any] = {
            "accion": "Buscar_Simple",
            "campo[0]": "SUBASTA.ESTADO",
            "dato[0]": "EJ",
            "campo[1]": "BIEN.TIPO",
            "dato[1]": "I",
            "sort_field[0]": "SUBASTA.FECHA_FIN_YMD",
            "sort_order[0]": "desc",
            "page_hits": min(max_results, 50),
        }
        if code:
            base["campo[2]"] = "BIEN.PROVINCIA"
            base["dato[2]"] = code
        estrategias.append(("avanzada", dict(base)))

        # Igual pero con la accion que usa el boton del formulario.
        buscar = dict(base, accion="Buscar")
        estrategias.append(("avanzada-buscar", buscar))

        # La accion de paginacion, que en este portal tambien lista.
        estrategias.append(("avanzada-mas", dict(base, accion="Mas")))

        # Busqueda simple: sin filtros de tipo, por si los campos han cambiado.
        simple: dict[str, Any] = {"accion": "Buscar_Simple", "page_hits": 50}
        if code:
            simple["campo[0]"] = "BIEN.PROVINCIA"
            simple["dato[0]"] = code
        estrategias.append(("simple", simple))

        # Sin ningun parametro: sirve para saber si el portal esta vivo.
        estrategias.append(("sin-filtros", {}))
        return estrategias

    def search(
        self,
        province: str | None = None,
        *,
        only_land: bool = True,
        max_results: int = 40,
    ) -> list[str]:
        """Identificadores de subasta que encajan con el filtro."""
        for _, ids, _ in self.search_attempts(province, max_results):
            if ids:
                return ids[:max_results]
        return []

    def search_attempts(
        self, province: str | None = None, max_results: int = 40
    ) -> list[tuple[str, list[str], dict[str, Any]]]:
        """Cada estrategia probada con lo que devolvio. Lo usa el diagnostico."""
        resultados: list[tuple[str, list[str], dict[str, Any]]] = []
        for nombre, params in self.search_strategies(province, max_results):
            info: dict[str, Any] = {"params": {k: str(v) for k, v in params.items()}}
            try:
                response = self.request(
                    "GET", f"{self.base_url}/subastas_ava.php", params=params
                )
            except SourceError as exc:
                info["error"] = str(exc)[:300]
                resultados.append((nombre, [], info))
                continue

            info["http_status"] = response.status_code
            info["bytes"] = len(response.text)
            info["final_url"] = str(response.url)
            if response.status_code != 200:
                resultados.append((nombre, [], info))
                continue

            ids = self.parse_result_ids(response.text)
            info["link_patterns"] = _count_patterns(response.text)
            if not ids:
                # Sin el cuerpo no hay forma de saber que devolvio el portal, y
                # remitir a otro diagnostico seria dar vueltas.
                info["body_excerpt"] = _excerpt(response.text)
            resultados.append((nombre, ids, info))
            if ids:
                break
        return resultados

    @staticmethod
    def province_code(province: str | None) -> str | None:
        """Codigo INE de la provincia. Sin el, la busqueda sale sin filtro."""
        return code_for(province)

    @staticmethod
    def parse_result_ids(html: str) -> list[str]:
        """Identificadores de subasta presentes en una página de resultados.

        Se buscan por patrón y no por estructura: el listado cambia de
        maquetación con frecuencia, pero el identificador tiene forma fija
        (SUB-JA-2026-123456). Se aceptan varias formas de enlace porque el
        portal no siempre usa el mismo nombre de parámetro.
        """
        found: list[str] = []
        for pattern in (r"idSub=([A-Z]{3}-[A-Z]{2}-\d{4}-\d+)",
                        r"idSub=([A-Z0-9\-]{6,})",
                        r"\b(SUB-[A-Z]{2}-\d{4}-\d+)\b"):
            found = re.findall(pattern, html)
            if found:
                break
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


def _count_patterns(html: str) -> dict[str, int]:
    """Cuantos enlaces de cada forma hay: dice si la pagina es de resultados."""
    return {
        "idSub": len(re.findall(r"idSub=", html)),
        "detalleSubasta": len(re.findall(r"detalleSubasta", html)),
        "SUB-xx-": len(re.findall(r"SUB-[A-Z]{2}-\d{4}", html)),
        "formulario": len(re.findall(r"<form", html, re.IGNORECASE)),
        "sin_resultados": len(re.findall(r"no se han encontrado|sin resultados",
                                         html, re.IGNORECASE)),
    }


def _excerpt(html: str, limit: int = 1200) -> str:
    """Texto visible de la pagina, sin etiquetas, para poder leerlo de un vistazo."""
    without_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                             flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", text).strip()[:limit]


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
