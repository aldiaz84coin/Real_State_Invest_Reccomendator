"""Subastas a traves de la API de datos abiertos del BOE.

El buscador del portal (subastas.boe.es) no es una API: no esta documentado,
sus parametros hay que adivinarlos leyendo el formulario y devolvia siempre la
misma pagina. La Agencia Estatal BOE si publica una API de datos abiertos,
documentada, sin clave y sin cuota, y por ahi pasa toda subasta antes de
existir en el portal: para celebrarse tiene que anunciarse en el Boletin.

  * Las subastas judiciales se publican en la **seccion IV** (Administracion
    de Justicia).
  * Las administrativas -Agencia Tributaria, Seguridad Social, ayuntamientos-
    en la **seccion V** (Anuncios).

De cada anuncio se saca el identificador de subasta (SUB-...), y con el la
ficha estructurada del portal, que si es una pagina estable y ya se sabe leer.
Asi el descubrimiento va por la via documentada y solo el detalle depende del
portal.

Documentacion: https://www.boe.es/datosabiertos/api/api.php
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterator

from app.sources.base import BaseSource, SourceError, SourceStatus

# Identificador de subasta tal y como aparece en el texto del anuncio.
AUCTION_ID = re.compile(r"\b(SUB-[A-Z]{2}-\d{4}-\d+)\b")

# Un anuncio de venta se reconoce por su titulo. Se compara sin acentos.
# «Subasta» sola dejaba fuera lo que mas suelo saca al mercado: ADIF, SEPES,
# el INVIED y los ayuntamientos enajenan por pliego, no por subasta
# electronica, y sus anuncios no llevan esa palabra en el titulo. Los terminos
# se mantienen especificos a proposito: anadir «licitacion» a secas metia en
# el recorrido las miles de licitaciones de obra y servicios de cada dia.
AUCTION_WORDS = (
    "subasta", "subastas", "enajenacion", "enajenar", "venta de bien",
    "venta de inmueble", "venta de parcela", "venta de solar",
    "venta de finca", "venta de suelo", "venta directa", "venta mediante",
)

# Secciones donde se publican. Se guardan como texto porque el sumario las
# identifica unas veces por codigo y otras por nombre.
AUCTION_SECTIONS = {"4", "iv", "v", "5", "administracion de justicia", "anuncios"}


def _normalize(text: str) -> str:
    import unicodedata

    limpio = unicodedata.normalize("NFKD", str(text or ""))
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", limpio).strip().lower()


class BoeSumarioSource(BaseSource):
    """API de sumarios del BOE: JSON abierto, documentado y sin credenciales."""

    key = "boe_sumario"
    name = "BOE · API de sumarios (datos abiertos)"
    kind = "listings"
    required = False
    docs_url = "https://www.boe.es/datosabiertos/api/api.php"
    licence = (
        "Datos abiertos de la Agencia Estatal BOE. Reutilización libre "
        "conforme a la Ley 37/2007, citando la fuente."
    )
    radius_note = "No usa radio: recorre los boletines de los últimos días."

    def sumario(self, dia: date) -> Any:
        """Sumario del boletín de un día concreto."""
        url = f"{self.settings.boe_api_url}/boe/sumario/{dia:%Y%m%d}"
        response = self.request("GET", url, headers={"Accept": "application/json"})
        if response.status_code == 404:
            # Domingos y festivos no hay boletín: no es un error.
            return None
        if response.status_code != 200:
            raise SourceError(f"BOE datos abiertos HTTP {response.status_code} en {url}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"El BOE no devolvió JSON en {url}: {exc}") from None

    @staticmethod
    def walk_items(payload: Any, seccion: str = "") -> Iterator[dict[str, Any]]:
        """Recorre el sumario y saca cada documento con su sección.

        Se busca por forma y no por ruta fija: el sumario anida secciones,
        departamentos, epígrafes y documentos, y esa anidación cambia según el
        día porque no todos los boletines traen los mismos niveles. Lo estable
        es que un documento es un objeto con `identificador` y `titulo`.
        """
        if isinstance(payload, dict):
            nombre = payload.get("nombre") or payload.get("titulo") or ""
            codigo = payload.get("codigo") or payload.get("num") or ""
            if "identificador" in payload and payload.get("titulo"):
                yield {**payload, "seccion": seccion}
                return
            actual = seccion
            if payload.get("codigo") or _normalize(nombre) in AUCTION_SECTIONS:
                actual = str(codigo or nombre) or seccion
            for valor in payload.values():
                if isinstance(valor, (dict, list)):
                    yield from BoeSumarioSource.walk_items(valor, actual)
        elif isinstance(payload, list):
            for elemento in payload:
                yield from BoeSumarioSource.walk_items(elemento, seccion)

    def auction_items(self, payload: Any) -> list[dict[str, Any]]:
        """Documentos del sumario que anuncian una venta de patrimonio."""
        encontrados: list[dict[str, Any]] = []
        vistos: set[str] = set()
        for item in self.walk_items(payload):
            titulo = _normalize(item.get("titulo"))
            if not any(palabra in titulo for palabra in AUCTION_WORDS):
                continue
            identificador = str(item.get("identificador") or "")
            if not identificador or identificador in vistos:
                continue
            vistos.add(identificador)
            encontrados.append({
                "identificador": identificador,
                "titulo": str(item.get("titulo"))[:400],
                "seccion": item.get("seccion", ""),
                "url_xml": self._absoluta(self._url(item, "Xml"))
                or self.xml_url(identificador),
                "url_html": self._absoluta(
                    self._url(item, "Htm") or self._url(item, "Html")
                ),
            })
        return encontrados

    @staticmethod
    def _url(item: dict[str, Any], sufijo: str) -> str:
        for clave, valor in item.items():
            if clave.lower().endswith(sufijo.lower()) and isinstance(valor, str):
                return valor
        return ""

    def _absoluta(self, url: str) -> str:
        """El sumario da unas urls absolutas y otras relativas al sitio."""
        if not url:
            return ""
        if url.startswith("http"):
            return url
        return "https://www.boe.es" + ("" if url.startswith("/") else "/") + url

    def xml_url(self, identificador: str) -> str:
        return f"{self.settings.boe_diario_url}/xml.php?id={identificador}"

    def announcement_xml(self, identificador: str, url: str = "") -> str:
        """XML del anuncio tal y como lo sirve el BOE.

        Se guarda el documento sin tocar, y no sólo su texto plano, porque la
        fuente que lee el suelo del anuncio necesita distinguir el cuerpo de
        los metadatos, y con el texto ya aplanado no hay forma de separarlos.
        """
        response = self.request("GET", url or self.xml_url(identificador))
        if response.status_code != 200:
            raise SourceError(
                f"BOE HTTP {response.status_code} en el anuncio {identificador}"
            )
        return response.text

    def announcement_text(self, identificador: str, url: str = "") -> str:
        """Texto completo del anuncio, que es donde va el número de subasta."""
        sin_etiquetas = re.sub(
            r"<[^>]+>", " ", self.announcement_xml(identificador, url)
        )
        return re.sub(r"\s+", " ", sin_etiquetas).strip()

    @staticmethod
    def auction_ids(texto: str) -> list[str]:
        """Identificadores de subasta citados en el anuncio."""
        unicos: list[str] = []
        for encontrado in AUCTION_ID.findall(texto or ""):
            if encontrado not in unicos:
                unicos.append(encontrado)
        return unicos

    def crawl(
        self, days: int = 14, max_items: int = 60, today: date | None = None
    ) -> dict[str, Any]:
        """Anuncios de venta de los últimos boletines, con su texto completo.

        Es el recorrido que comparten las dos fuentes que salen del Boletín: la
        que busca el identificador de subasta electrónica y la que lee el suelo
        del propio anuncio. Se hace una vez porque descargar dos veces los cien
        y pico anuncios de un día es lo que antes hacía que el BOE empezara a
        cortar peticiones a mitad del recorrido.

        El resumen de cada día distingue las tres cosas que se confundían en un
        único cero: cuántos anuncios había, cuántos no se pudieron descargar y
        cuántos se descargaron sin traer identificador de subasta. Sin esa
        distinción, «0 identificadas» no decía si fallaba la red, el filtro o
        es que ese día no había ninguna.
        """
        hoy = today or date.today()
        anuncios: list[dict[str, Any]] = []
        dias: list[dict[str, Any]] = []

        for retroceso in range(days):
            dia = hoy - timedelta(days=retroceso)
            resumen: dict[str, Any] = {"date": dia.isoformat()}
            try:
                payload = self.sumario(dia)
            except SourceError as exc:
                resumen["error"] = str(exc)[:200]
                dias.append(resumen)
                continue

            if payload is None:
                resumen["published"] = False
                dias.append(resumen)
                continue

            del_dia = self.auction_items(payload)
            resumen["published"] = True
            resumen["auction_announcements"] = len(del_dia)
            fallos = con_id = sin_id = 0
            ultimo_fallo = ""

            for anuncio in del_dia:
                if len(anuncios) >= max_items:
                    break
                try:
                    xml = self.announcement_xml(
                        anuncio["identificador"], anuncio["url_xml"]
                    )
                except SourceError as exc:
                    fallos += 1
                    ultimo_fallo = str(exc)[:160]
                    continue
                texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)).strip()
                ids = self.auction_ids(texto)
                con_id += 1 if ids else 0
                sin_id += 0 if ids else 1
                anuncios.append(
                    {**anuncio, "xml": xml, "texto": texto, "auction_ids": ids}
                )

            resumen["downloaded"] = con_id + sin_id
            resumen["download_errors"] = fallos
            resumen["with_auction_id"] = con_id
            resumen["without_auction_id"] = sin_id
            if ultimo_fallo:
                resumen["last_error"] = ultimo_fallo
            # Los títulos del día explican de un vistazo qué se está mirando
            # cuando el recuento de candidatas no cuadra con lo esperado.
            resumen["sample_titles"] = [a["titulo"][:120] for a in del_dia[:4]]
            dias.append(resumen)
            if len(anuncios) >= max_items:
                break

        return {"announcements": anuncios, "days": dias}

    def recent_auction_ids(
        self, days: int = 14, max_results: int = 40, today: date | None = None
    ) -> dict[str, Any]:
        """Subastas electrónicas anunciadas en los boletines recientes.

        Sólo las judiciales, notariales y de la Agencia Tributaria llevan
        identificador `SUB-`: son las que se celebran en el Portal de Subastas.
        Las ventas por pliego de ADIF, el INVIED o un ayuntamiento no lo tienen
        y no aparecen aquí; ésas las recoge `BoeAnunciosSource` leyendo el
        texto del anuncio.
        """
        recorrido = self.crawl(days=days, max_items=max(max_results * 4, 40),
                               today=today)
        ids: list[str] = []
        for anuncio in recorrido["announcements"]:
            for identificador in anuncio.get("auction_ids", []):
                if identificador not in ids and len(ids) < max_results:
                    ids.append(identificador)

        for resumen in recorrido["days"]:
            resumen["auction_ids"] = resumen.get("with_auction_id", 0)

        return {
            "ids": ids[:max_results],
            "days": recorrido["days"],
            "announcements": recorrido["announcements"],
        }

    def check(self) -> SourceStatus:
        dia = date.today()
        # El boletín de hoy puede no estar publicado todavía a primera hora.
        return self._timed_probe(
            f"{self.settings.boe_api_url}/boe/sumario/{dia - timedelta(days=1):%Y%m%d}",
            headers={"Accept": "application/json"},
        )
