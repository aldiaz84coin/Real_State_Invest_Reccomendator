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

# Un anuncio de subasta se reconoce por su titulo. Se compara sin acentos.
AUCTION_WORDS = ("subasta", "subastas")

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
        """Documentos del sumario que anuncian una subasta."""
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

    def announcement_text(self, identificador: str, url: str = "") -> str:
        """Texto completo del anuncio, que es donde va el número de subasta."""
        response = self.request("GET", url or self.xml_url(identificador))
        if response.status_code != 200:
            raise SourceError(
                f"BOE HTTP {response.status_code} en el anuncio {identificador}"
            )
        sin_etiquetas = re.sub(r"<[^>]+>", " ", response.text)
        return re.sub(r"\s+", " ", sin_etiquetas).strip()

    @staticmethod
    def auction_ids(texto: str) -> list[str]:
        """Identificadores de subasta citados en el anuncio."""
        unicos: list[str] = []
        for encontrado in AUCTION_ID.findall(texto or ""):
            if encontrado not in unicos:
                unicos.append(encontrado)
        return unicos

    def recent_auction_ids(
        self, days: int = 14, max_results: int = 40, today: date | None = None
    ) -> dict[str, Any]:
        """Recorre los boletines recientes y devuelve las subastas anunciadas.

        Se va hacia atrás desde hoy porque una subasta se anuncia una vez, el
        día en que se abre; el portal es quien mantiene el estado, y el BOE
        quien deja constancia de que existe.
        """
        hoy = today or date.today()
        ids: list[str] = []
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

            anuncios = self.auction_items(payload)
            resumen["published"] = True
            resumen["auction_announcements"] = len(anuncios)
            encontrados = 0
            for anuncio in anuncios:
                try:
                    texto = self.announcement_text(
                        anuncio["identificador"], anuncio["url_xml"]
                    )
                except SourceError:
                    continue
                for identificador in self.auction_ids(texto):
                    if identificador not in ids:
                        ids.append(identificador)
                        encontrados += 1
                if len(ids) >= max_results:
                    break
            resumen["auction_ids"] = encontrados
            dias.append(resumen)
            if len(ids) >= max_results:
                break

        return {"ids": ids[:max_results], "days": dias}

    def check(self) -> SourceStatus:
        dia = date.today()
        # El boletín de hoy puede no estar publicado todavía a primera hora.
        return self._timed_probe(
            f"{self.settings.boe_api_url}/boe/sumario/{dia - timedelta(days=1):%Y%m%d}",
            headers={"Accept": "application/json"},
        )
