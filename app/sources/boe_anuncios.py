"""Solares y fincas puestos a la venta por organismos públicos, leídos del BOE.

Nace de un fallo concreto del descubrimiento anterior: el sumario encontraba
más de cien anuncios de subasta al día y no convertía ninguno en candidata.
El motivo no era la red ni el parseo, sino el camino elegido. Se hacía

    sumario del BOE  ->  identificador SUB-...  ->  ficha del portal

y sólo llegan a tener identificador `SUB-` las subastas **electrónicas** que se
celebran en el Portal de Subastas: las judiciales, las notariales y las de la
Agencia Tributaria. Las que más suelo sacan al mercado no se celebran ahí:

  * el INVIED (Defensa) vende sus cuarteles y solares por «subasta pública con
    proposición económica al alza en sobre cerrado»;
  * ADIF y SEPES enajenan parcelas sobrantes por pliego;
  * los ayuntamientos sacan solares del patrimonio municipal de suelo.

Todas ésas se anuncian en la sección V del Boletín y **no** llevan `SUB-`, así
que el filtro las descartaba una por una. Aquí se leen del propio anuncio, que
es un XML estable, documentado y sin clave: el organismo, el pliego, el tipo de
licitación y la descripción de la finca vienen en el texto.

Documentación: https://www.boe.es/datosabiertos/api/api.php
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from app.provinces import PROVINCES, code_for, spellings
from app.sources.base import BaseSource, SourceError, SourceStatus

# --- Reconocer que lo que se vende es suelo --------------------------------
# Se compara sobre texto sin acentos. «Finca urbana» queda fuera a proposito:
# describe igual un solar que un piso, y sin mas contexto no se puede decidir.
LAND_WORDS = (
    "solar", "solares", "parcela", "parcelas", "terreno", "terrenos",
    "finca rustica", "fincas rusticas", "suelo urbano", "suelo urbanizable",
    "suelo industrial", "suelo residencial", "sin edificar", "no edificado",
    "descampado", "huerta", "olivar", "vinedo",
)

# Lo que NO interesa aunque el anuncio hable de inmuebles. Si el titulo dice
# vivienda o garaje y no menciona suelo, no es una candidata para construir.
BUILT_WORDS = (
    "vivienda", "viviendas", "piso", "pisos", "chalet", "apartamento",
    "garaje", "plaza de garaje", "trastero", "local comercial", "nave",
    "vehiculo", "vehiculos", "maquinaria", "buque",
)

# Titulos que anuncian una venta de patrimonio publico. El descubrimiento
# anterior solo miraba «subasta» y se dejaba fuera la enajenacion por concurso
# o por pliego, que es como venden ADIF, SEPES y buena parte de los
# ayuntamientos.
SALE_WORDS = (
    "subasta", "enajenacion", "enajenar", "venta de", "adjudicacion",
    "licitacion", "concurso", "concurrencia", "alienacion",
)

# Etiquetas con las que el anuncio nombra el precio de partida, en orden de
# preferencia: lo que hay que pagar manda sobre lo que vale.
PRICE_LABELS = (
    "tipo de licitacion", "tipo de la licitacion", "tipo minimo",
    "tipo de subasta", "tipo de la subasta", "tipo de salida",
    "precio de salida", "precio minimo", "precio de licitacion",
    "importe de salida", "presupuesto base de licitacion",
    "valor de tasacion", "tasacion", "valoracion", "tipo",
)

# Un importe en euros tal y como lo escribe el BOE: «1.234.567,89 euros».
AMOUNT = r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"
AMOUNT_RE = re.compile(
    AMOUNT + r"\s*(?:euros?|eur\b|€)", re.IGNORECASE
)

# Superficies. Se aceptan hectareas y areas porque las fincas rusticas se
# describen asi, y confundirlas cambia el precio por metro en dos ordenes de
# magnitud.
AREA_UNITS: tuple[tuple[str, float], ...] = (
    (r"hect[aá]reas?|\bhas?\b", 10_000.0),
    (r"[aá]reas?\b", 100.0),
    (r"m2|m²|metros cuadrados|metros\s+cuadrados", 1.0),
)

CADASTRAL_RE = re.compile(r"\b([0-9A-Z]{20})\b")
MUNICIPALITY_RE = re.compile(
    r"(?:t[eé]rmino municipal de|t[eé]rmino de|sit[oa]s? en|"
    r"sit[oa]s? en el|ubicad[oa]s? en|localidad de|municipio de|en el municipio de)"
    r"\s+([A-ZÁÉÍÓÚÑ][\wÀ-ÿ'\-\.]*"
    r"(?:\s+(?:de|del|la|las|el|los|d[eo]s?|i)\b)?"
    r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÀ-ÿ'\-\.]*){0,3})"
)


def _normalize(text: Any) -> str:
    """Minúsculas, sin acentos y con los espacios colapsados."""
    limpio = unicodedata.normalize("NFKD", str(text or ""))
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", limpio).strip().lower()


def _amount(text: str) -> float | None:
    """Primer importe en euros de un fragmento."""
    match = AMOUNT_RE.search(text)
    if not match:
        return None
    crudo = match.group(1).replace(".", "").replace(",", ".")
    try:
        valor = float(crudo)
    except ValueError:
        return None
    return valor if valor > 0 else None


def parse_price(texto: str) -> tuple[float | None, str]:
    """Precio de partida y la etiqueta con la que venía nombrado.

    Se busca por etiqueta y no el primer número del anuncio: un edicto de
    subasta cita también la deuda reclamada, las costas y el depósito, y
    quedarse con el primer importe daba precios sin ninguna relación con la
    finca. Devuelve además la etiqueta para poder auditarlo desde el panel.
    """
    plano = _normalize(texto)
    for etiqueta in PRICE_LABELS:
        for match in re.finditer(re.escape(etiqueta), plano):
            # El importe va justo detrás de la etiqueta, a veces con dos
            # puntos, un guion o la palabra «asciende a» por medio.
            cola = plano[match.end(): match.end() + 160]
            valor = _amount(cola)
            if valor:
                return valor, etiqueta
    return None, ""


def parse_area(texto: str) -> float | None:
    """Superficie en metros cuadrados, con las unidades del Registro.

    Gana la superficie que aparece **antes** en el texto, no la primera unidad
    que se pruebe: un anuncio que describe un solar de 800 m2 y cita luego el
    monte de 3 hectáreas del lindero se leía como 30.000 m2 si las hectáreas se
    comprobaban primero, y el precio por metro salía cuarenta veces más barato.
    """
    plano = _normalize(texto)
    mejor: tuple[int, float] | None = None
    for patron, factor in AREA_UNITS:
        match = re.search(AMOUNT + r"\s*(?:" + patron + r")", plano)
        if not match:
            continue
        crudo = match.group(1).replace(".", "").replace(",", ".")
        try:
            valor = float(crudo)
        except ValueError:
            continue
        if valor > 0 and (mejor is None or match.start() < mejor[0]):
            mejor = (match.start(), valor * factor)
    return mejor[1] if mejor else None


def is_land(texto: str) -> bool:
    """¿El anuncio vende suelo, y no un piso o un garaje?

    Manda la mención explícita a suelo: un lote puede incluir una vivienda y
    además la parcela, y descartarlo por nombrar la vivienda dejaría fuera
    justo los solares con edificación a demoler, que es donde está el margen.
    """
    plano = _sin_referencias(texto)
    return any(re.search(r"\b" + palabra + r"\b", plano) for palabra in LAND_WORDS)


def _sin_referencias(texto: str) -> str:
    """Texto sin las coletillas catastrales que nombran suelo sin venderlo.

    «Parcela catastral» y «referencia catastral de la parcela» salen en el
    anuncio de un garaje igual que en el de un solar: dejarlas dentro hacía que
    cualquier plaza de aparcamiento pasara el filtro de suelo.
    """
    plano = _normalize(texto)
    return re.sub(r"(?:referencia catastral(?: de la parcela)?|parcela catastral)",
                  " ", plano)


def only_built(texto: str) -> bool:
    """Anuncios que sólo hablan de inmueble construido."""
    plano = _normalize(texto)
    tiene_construido = any(
        re.search(r"\b" + palabra + r"\b", plano) for palabra in BUILT_WORDS
    )
    return tiene_construido and not is_land(texto)


def is_sale(titulo: str) -> bool:
    """¿El título anuncia una venta de patrimonio, y no otro trámite?"""
    plano = _normalize(titulo)
    return any(palabra in plano for palabra in SALE_WORDS)


def parse_province(texto: str) -> str:
    """Provincia citada en el anuncio, con el nombre oficial del INE.

    Se busca cada grafía admitida y gana la que aparezca antes: los anuncios
    suelen nombrar la provincia de la finca al principio y la del juzgado o el
    organismo después.
    """
    plano = _normalize(texto)
    mejor: tuple[int, str] = (len(plano) + 1, "")
    for codigo, nombre in PROVINCES.items():
        for grafia in spellings(codigo):
            posicion = plano.find(_normalize(grafia))
            if posicion >= 0 and posicion < mejor[0]:
                mejor = (posicion, nombre)
    return mejor[1]


def parse_municipality(texto: str) -> str:
    """Municipio de la finca, cuando el anuncio lo nombra de forma reconocible."""
    match = MUNICIPALITY_RE.search(texto)
    if not match:
        return ""
    nombre = re.sub(r"\s+", " ", match.group(1)).strip(" .,;:")
    # Cortar en la primera palabra funcional suelta evita arrastrar media frase.
    nombre = re.split(r"\s+(?:con|de la provincia|provincia|inscrit|finca|y\b)", nombre)[0]
    return nombre.strip(" .,;:")[:120]


def parse_cadastral(texto: str) -> str | None:
    """Referencia catastral: veinte caracteres alfanuméricos."""
    for candidato in CADASTRAL_RE.findall(str(texto).upper()):
        # Veinte dígitos seguidos casi siempre son un número de finca o una
        # cuenta, no una referencia catastral: ésta siempre lleva letras.
        if re.search(r"[A-Z]", candidato) and re.search(r"\d", candidato):
            return candidato
    return None


class BoeAnunciosSource(BaseSource):
    """Ventas de suelo público leídas del texto del anuncio del BOE."""

    key = "boe_anuncios"
    name = "BOE · Anuncios de venta de suelo público"
    kind = "listings"
    required = False
    docs_url = "https://www.boe.es/datosabiertos/api/api.php"
    licence = (
        "Datos abiertos de la Agencia Estatal BOE. Reutilización libre "
        "conforme a la Ley 37/2007, citando la fuente."
    )
    radius_note = (
        "No usa radio: lee los anuncios de los últimos boletines y filtra por "
        "la provincia que nombra el propio anuncio."
    )

    @property
    def base_url(self) -> str:
        return self.settings.boe_diario_url

    def announcement_url(self, identificador: str) -> str:
        return f"{self.base_url}/xml.php?id={identificador}"

    def public_url(self, identificador: str) -> str:
        return f"{self.base_url}/txt.php?id={identificador}"

    def fetch(self, identificador: str, url: str = "") -> dict[str, Any]:
        """Anuncio completo: metadatos del XML y texto sin etiquetas."""
        response = self.request("GET", url or self.announcement_url(identificador))
        if response.status_code != 200:
            raise SourceError(
                f"BOE HTTP {response.status_code} en el anuncio {identificador}"
            )
        return self.parse_xml(response.text, identificador)

    @classmethod
    def parse_xml(cls, xml: str, identificador: str) -> dict[str, Any]:
        """Metadatos y texto de un anuncio.

        Se lee con expresiones y no con un parser de XML porque el BOE sirve
        estos documentos con entidades y fragmentos HTML dentro de `<texto>`,
        y un parser estricto se rompía en los anuncios con tablas de lotes.
        """
        return {
            "identificador": identificador,
            "titulo": _tag(xml, "titulo"),
            "departamento": _tag(xml, "departamento"),
            "seccion": _tag(xml, "seccion"),
            "fecha_publicacion": _tag(xml, "fecha_publicacion"),
            "texto": _plain_text(_between(xml, "texto") or xml),
            "url": public_announcement_url(identificador),
        }

    # -- conversión al esquema de la aplicación --------------------------

    def normalize(
        self, anuncio: dict[str, Any], *, lote: str = "", texto: str = ""
    ) -> dict[str, Any] | None:
        """Traduce un anuncio (o uno de sus lotes) a una candidata.

        Sin precio de partida o sin superficie no hay nada que comparar: el
        criterio de la aplicación es el precio por metro, así que un anuncio al
        que le falte cualquiera de los dos se descarta en vez de inventarlo.
        """
        cuerpo = texto or str(anuncio.get("texto") or "")
        completo = f"{anuncio.get('titulo', '')} {cuerpo}"

        precio, etiqueta = parse_price(cuerpo)
        superficie = parse_area(cuerpo)
        if not precio or not superficie:
            return None

        identificador = str(anuncio.get("identificador") or "")
        externo = f"{identificador}-{lote}" if lote else identificador
        provincia = parse_province(completo)

        return {
            "source": self.key,
            "external_id": externo,
            "url": anuncio.get("url") or self.public_url(identificador),
            "title": (anuncio.get("titulo") or "Venta de suelo público")[:200],
            "description": cuerpo[:4000],
            "price_eur": precio,
            "area_m2": superficie,
            "price_eur_m2": round(precio / superficie, 2),
            "lat": None,
            "lon": None,
            "coords_precision": "missing",
            "address": "",
            "municipality_name": parse_municipality(cuerpo),
            "province": provincia,
            "land_type": "suelo público",
            "cadastral_ref": parse_cadastral(cuerpo),
            "raw": {
                "identificador": identificador,
                "lote": lote,
                "departamento": anuncio.get("departamento", ""),
                "seccion": anuncio.get("seccion", ""),
                "fecha_publicacion": anuncio.get("fecha_publicacion", ""),
                "price_label": etiqueta,
            },
        }

    def candidates(self, anuncio: dict[str, Any]) -> list[dict[str, Any]]:
        """Candidatas de un anuncio: una por lote, o una sola si no los hay.

        Un mismo anuncio del INVIED saca veinticinco propiedades a la vez, cada
        una con su tipo de licitación. Tratarlo como un solo bien mezclaba el
        precio de un lote con la superficie de otro y producía precios por
        metro sin sentido.
        """
        texto = str(anuncio.get("texto") or "")
        titulo = str(anuncio.get("titulo") or "")
        if not is_sale(titulo) and not is_sale(texto[:400]):
            return []

        salida: list[dict[str, Any]] = []
        for etiqueta, fragmento in split_lots(texto):
            if not is_land(f"{titulo} {fragmento}") or only_built(fragmento):
                continue
            candidata = self.normalize(anuncio, lote=etiqueta, texto=fragmento)
            if candidata:
                salida.append(candidata)
        return salida

    def from_crawl(
        self,
        anuncios: list[dict[str, Any]],
        *,
        province: str | None = None,
        max_results: int = 40,
    ) -> dict[str, Any]:
        """Candidatas a partir del recorrido de boletines del sumario.

        Recibe los anuncios ya descargados en vez de volver a pedirlos: son las
        mismas peticiones, y hacerlas dos veces era lo que agotaba la paciencia
        del BOE a mitad de recorrido.

        El filtro de provincia se aplica aquí y no en la consulta porque el
        Boletín es nacional: no hay forma de pedirle sólo Cantabria, y la
        provincia sale del propio texto del anuncio.
        """
        pedida = code_for(province) if province else None
        candidatas: list[dict[str, Any]] = []
        descartes = {"no_es_venta": 0, "no_es_suelo": 0,
                     "sin_precio_o_superficie": 0, "otra_provincia": 0}

        for crudo in anuncios:
            anuncio = self._as_announcement(crudo)
            titulo = str(anuncio.get("titulo") or "")
            texto = str(anuncio.get("texto") or "")
            if not is_sale(titulo) and not is_sale(texto[:400]):
                descartes["no_es_venta"] += 1
                continue
            if not is_land(f"{titulo} {texto}"):
                descartes["no_es_suelo"] += 1
                continue

            del_anuncio = self.candidates(anuncio)
            if not del_anuncio:
                descartes["sin_precio_o_superficie"] += 1
                continue
            for candidata in del_anuncio:
                if pedida and code_for(candidata.get("province")) != pedida:
                    descartes["otra_provincia"] += 1
                    continue
                candidata["source_name"] = self.name
                candidatas.append(candidata)
                if len(candidatas) >= max_results:
                    break
            if len(candidatas) >= max_results:
                break

        return {"candidates": candidatas, "discarded": descartes}

    @staticmethod
    def _as_announcement(crudo: dict[str, Any]) -> dict[str, Any]:
        """Normaliza lo que llega del sumario a la forma que usa esta fuente.

        El recorrido guarda el XML sin tocar cuando puede; si sólo hay texto
        plano se trabaja con él, que es lo que pasa con los anuncios que ya
        venían cacheados de una versión anterior.
        """
        identificador = str(crudo.get("identificador") or "")
        if crudo.get("xml"):
            anuncio = BoeAnunciosSource.parse_xml(str(crudo["xml"]), identificador)
            # El título del sumario es el bueno: el del XML viene recortado.
            if crudo.get("titulo"):
                anuncio["titulo"] = str(crudo["titulo"])
            return anuncio
        return {
            "identificador": identificador,
            "titulo": str(crudo.get("titulo") or ""),
            "departamento": str(crudo.get("departamento") or ""),
            "seccion": str(crudo.get("seccion") or ""),
            "fecha_publicacion": str(crudo.get("fecha_publicacion") or ""),
            "texto": str(crudo.get("texto") or ""),
            "url": public_announcement_url(identificador),
        }

    def check(self) -> SourceStatus:
        """Se comprueba contra un sumario reciente: es la misma API."""
        from datetime import timedelta

        ayer = date.today() - timedelta(days=1)
        return self._timed_probe(
            f"{self.settings.boe_api_url}/boe/sumario/{ayer:%Y%m%d}",
            headers={"Accept": "application/json"},
        )


def public_announcement_url(identificador: str) -> str:
    """Página del anuncio en boe.es, la que se enseña al usuario."""
    return f"https://www.boe.es/diario_boe/txt.php?id={identificador}"


LOT_RE = re.compile(
    r"(?:^|[\.\s;])(?:lote|finca|inmueble|propiedad|parcela)\s*"
    r"(?:n[uú]m(?:ero)?\.?\s*|n[ºo°]\.?\s*)?(\d{1,3})\s*[\.\-:–)]",
    re.IGNORECASE,
)


def split_lots(texto: str) -> list[tuple[str, str]]:
    """Parte el anuncio en lotes numerados, si los tiene.

    Devuelve pares (etiqueta, fragmento). Cuando no hay numeración -que es lo
    normal en las subastas judiciales, de una sola finca- devuelve el anuncio
    entero con etiqueta vacía, para que quien lo llame no tenga que distinguir
    los dos casos.
    """
    marcas = list(LOT_RE.finditer(texto or ""))
    if len(marcas) < 2:
        return [("", texto or "")]

    trozos: list[tuple[str, str]] = []
    for indice, marca in enumerate(marcas):
        fin = marcas[indice + 1].start() if indice + 1 < len(marcas) else len(texto)
        trozos.append((marca.group(1), texto[marca.start():fin]))
    return trozos


def _between(xml: str, etiqueta: str) -> str:
    match = re.search(
        rf"<{etiqueta}[^>]*>(.*?)</{etiqueta}>", xml, re.DOTALL | re.IGNORECASE
    )
    return match.group(1) if match else ""


def _tag(xml: str, etiqueta: str) -> str:
    """Contenido de una etiqueta del XML, ya sin marcado ni espacios sobrantes."""
    return _plain_text(_between(xml, etiqueta))[:400]


def _plain_text(fragmento: str) -> str:
    """Texto legible: fuera etiquetas, entidades y espacios repetidos."""
    import html

    sin_etiquetas = re.sub(r"<[^>]+>", " ", fragmento or "")
    return re.sub(r"\s+", " ", html.unescape(sin_etiquetas)).strip()


__all__ = [
    "BoeAnunciosSource",
    "is_land",
    "is_sale",
    "only_built",
    "parse_area",
    "parse_cadastral",
    "parse_municipality",
    "parse_price",
    "parse_province",
    "split_lots",
]
