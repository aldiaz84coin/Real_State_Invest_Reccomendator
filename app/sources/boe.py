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

# Ruta del buscador. consultas_subastas_ava.php devolvia 404: no existe.
SEARCH_PATH = "subastas_ava.php"

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
        self, province: str | None, max_results: int, form: dict[str, Any] | None = None
    ) -> list[tuple[str, str, str, dict[str, Any]]]:
        """Metodo, ruta y parametros a probar, del mas especifico al mas simple.

        Cuando se ha podido leer el formulario, la primera estrategia es
        replicarlo: se manda lo mismo que mandaria el navegador. Detras van las
        combinaciones escritas a mano, que se conservan por si el portal deja
        de servir el formulario.
        """
        code = self.province_code(province)
        estrategias: list[tuple[str, str, str, dict[str, Any]]] = []

        if form:
            del_formulario = search_params_from_form(form, code, max_results)
            if del_formulario:
                estrategias.append(("formulario-get", "GET", SEARCH_PATH, del_formulario))
                estrategias.append(("formulario-post", "POST", SEARCH_PATH, del_formulario))

        # Los nombres de campo salen del formulario real: la provincia va en
        # dato[8], no en dato[2] como se venia mandando.
        a_mano: dict[str, Any] = {
            "accion": "Buscar",
            "page_hits": 50,
            "sort_field[0]": "SUBASTA.FECHA_FIN",
            "sort_order[0]": "desc",
        }
        if code:
            a_mano["dato[8]"] = code
        estrategias.append(("a-mano-get", "GET", SEARCH_PATH, dict(a_mano)))
        estrategias.append(("a-mano-post", "POST", SEARCH_PATH, dict(a_mano)))

        # Sin ningun filtro: si aqui salen subastas, el problema son los
        # parametros; si no sale ninguna, es el acceso o el parseo.
        estrategias.append(("sin-filtros", "GET", SEARCH_PATH, {}))
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
        """Cada estrategia probada con lo que devolvio. Lo usa el diagnostico.

        Todo ocurre dentro de una misma sesion HTTP, y se empieza por cargar el
        formulario: el portal entrega ahi su cookie y la exige despues, y de esa
        misma respuesta se saca que parametros admite. Con un cliente nuevo por
        peticion, la busqueda llegaba sin sesion y con los campos inventados.
        """
        resultados: list[tuple[str, list[str], dict[str, Any]]] = []
        formulario: dict[str, Any] | None = None

        with self.session() as client:
            calentamiento: dict[str, Any] = {}
            try:
                inicial = self.request(
                    "GET", f"{self.base_url}/{SEARCH_PATH}", client=client
                )
                formulario = parse_form(inicial.text)
                calentamiento = {
                    "http_status": inicial.status_code,
                    "bytes": len(inicial.text),
                    "cookies": sorted(client.cookies.keys()),
                    "form_fields": len(formulario.get("fields") or {}),
                    "province_slot": province_slot(formulario),
                }
            except SourceError as exc:
                calentamiento = {"error": str(exc)[:200]}
            resultados.append(("sesion-inicial", [], calentamiento))

            estrategias = self.search_strategies(province, max_results, formulario)
            for nombre, metodo, ruta, params in estrategias:
                info: dict[str, Any] = {
                    "method": metodo,
                    "path": ruta,
                    "params": {k: str(v) for k, v in params.items()},
                }
                envio = {"data": params} if metodo == "POST" else {"params": params}
                try:
                    response = self.request(
                        metodo, f"{self.base_url}/{ruta}", client=client, **envio
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
                    # Sin el cuerpo no hay forma de saber que devolvio el
                    # portal, y remitir a otro diagnostico seria dar vueltas.
                    info["body_excerpt"] = _excerpt(response.text)
                resultados.append((nombre, ids, info))
                if ids:
                    break

        return resultados

    def form_fields(self, path: str = SEARCH_PATH) -> dict[str, Any]:
        """Campos y valores que admite el formulario de busqueda del portal.

        Existe porque los parametros de este portal no estan documentados en
        ninguna parte: en vez de seguir probando combinaciones a ciegas, se lee
        el propio formulario y se ve que nombres y que valores acepta.
        """
        response = self.request("GET", f"{self.base_url}/{path}")
        if response.status_code != 200:
            raise SourceError(f"BOE HTTP {response.status_code} en {path}")
        return parse_form(response.text)

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
        return self._timed_probe(f"{self.base_url}/{SEARCH_PATH}")


class _FormParser(HTMLParser):
    """Lee el formulario de busqueda: campos, valores por defecto y opciones.

    Es la forma de saber que admite este portal sin documentacion, que no la
    hay. Guarda tambien el valor de cada campo oculto, porque son los que
    emparejan cada `dato[N]` con el `campo[N]` que dice a que se refiere: sin
    ellos, mandar `dato[8]=39` no significa nada.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, dict[str, Any]] = {}
        self.submits: dict[str, str] = {}
        self.action: str = ""
        self.method: str = ""
        self._current: str | None = None
        self._option: dict[str, str] | None = None

    def _campo(self, nombre: str) -> dict[str, Any]:
        return self.fields.setdefault(nombre, {"value": "", "options": []})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        atributos = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self.action = atributos.get("action", "")
            self.method = (atributos.get("method") or "get").lower()
        elif tag == "select":
            self._current = atributos.get("name") or atributos.get("id") or ""
            self._campo(self._current)
        elif tag == "option" and self._current is not None:
            self._option = {"value": atributos.get("value", ""), "label": ""}
            if "selected" in atributos:
                self._campo(self._current)["value"] = atributos.get("value", "")
        elif tag == "input":
            nombre = atributos.get("name")
            if not nombre:
                return
            if atributos.get("type") in ("submit", "button", "image"):
                self.submits[nombre] = atributos.get("value", "")
                return
            if atributos.get("type") in ("checkbox", "radio") and "checked" not in atributos:
                self._campo(nombre)
                return
            self._campo(nombre)["value"] = atributos.get("value", "")

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option["label"] += data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option is not None and self._current is not None:
            self._campo(self._current)["options"].append(self._option)
            self._option = None
        elif tag == "select":
            self._current = None


def parse_form(html: str) -> dict[str, Any]:
    """Formulario del portal en forma utilizable."""
    parser = _FormParser()
    parser.feed(html)
    return {
        "fields": parser.fields,
        "submits": parser.submits,
        "action": parser.action,
        "method": parser.method,
    }


def province_slot(form: dict[str, Any]) -> str | None:
    """Nombre del campo cuyo desplegable son las provincias.

    Se busca por contenido y no por posicion: el portal lo tiene hoy en
    `dato[8]`, pero ese numero es un detalle de maquetacion que puede cambiar
    con cualquier retoque del formulario. Se reconoce porque sus opciones son
    los codigos del INE, que si son estables.
    """
    muestra = {"39", "28", "08", "46"}
    for nombre, info in form.get("fields", {}).items():
        valores = {o.get("value", "") for o in info.get("options", [])}
        if muestra <= valores:
            return nombre
    return None


def search_params_from_form(
    form: dict[str, Any], province_code: str | None, max_results: int
) -> dict[str, Any] | None:
    """Parametros de busqueda construidos a partir del propio formulario.

    Es la unica forma fiable de acertar aqui: se envia lo mismo que enviaria el
    navegador -incluidos los campos ocultos que emparejan cada dato con su
    campo- y solo se cambia la provincia, el tamano de pagina y el orden.
    Adivinar los nombres a mano fue lo que hizo que la busqueda devolviera
    siempre cero.
    """
    campos = form.get("fields") or {}
    if not campos:
        return None

    params: dict[str, Any] = {
        nombre: info.get("value", "")
        for nombre, info in campos.items()
        if info.get("value")
    }

    slot = province_slot(form)
    if province_code:
        if slot is None:
            return None
        params[slot] = province_code
    elif slot is not None:
        params.pop(slot, None)

    # page_hits y sort_field solo admiten los valores de su desplegable: los
    # que mandaba antes -25 y SUBASTA.FECHA_FIN_YMD- no estan en la lista.
    params.update(_opcion_valida(campos, "page_hits", str(max_results), "50"))
    params.update(_opcion_valida(campos, "sort_field[0]", None, "SUBASTA.FECHA_FIN"))
    params.update(_opcion_valida(campos, "sort_order[0]", None, "desc"))

    for nombre, valor in (form.get("submits") or {}).items():
        params.setdefault(nombre, valor)
    params.setdefault("accion", "Buscar")
    return params


def _opcion_valida(
    campos: dict[str, Any], nombre: str, preferido: str | None, respaldo: str
) -> dict[str, str]:
    """Elige un valor que el desplegable admita de verdad."""
    info = campos.get(nombre)
    if info is None:
        return {}
    permitidos = [o.get("value", "") for o in info.get("options", []) if o.get("value")]
    if not permitidos:
        return {nombre: preferido or respaldo}
    if preferido and preferido in permitidos:
        return {nombre: preferido}
    if respaldo in permitidos:
        return {nombre: respaldo}
    return {nombre: permitidos[0]}


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
