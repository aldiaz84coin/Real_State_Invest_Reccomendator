"""Precios y demanda turística a partir de fuentes públicas del INE.

InsideAirbnb sólo publica volcados de una docena de ciudades españolas
(Barcelona, Madrid, Málaga, Mallorca, Menorca, Sevilla, Valencia, Girona y
poco más), así que en una parcela de Noja o de Llanes —que es justo el perfil
que busca esta aplicación— no hay ningún dato y el plan de negocio caía en
valores de respaldo.

Aquí se añade la cobertura que falta con dos estadísticas del INE, gratuitas,
sin clave y con toda España dentro:

  * **Viviendas turísticas** (experimental, tablas 39363 municipios y 39364
    provincias). El propio INE la construye rastreando las tres plataformas
    más usadas, así que mide la oferta real de alquiler turístico, no la
    reglada. Da viviendas y plazas por municipio: la intensidad turística.
  * **Ocupación en apartamentos turísticos** (EOAP). Da grado de ocupación y
    tarifa media por zona y punto turístico, con serie mensual, que es lo que
    permite estimar la estacionalidad.

No sustituyen a InsideAirbnb donde éste llega —ahí hay precio por anuncio y
calendario real—, lo complementan donde no llega, que es casi toda España.
"""
from __future__ import annotations

import re
from typing import Any

from app.sources.base import BaseSource, SourceError, SourceStatus

# Tablas del sistema Tempus3. Son estables y publicas; se dejan como constante
# para poder sustituirlas si el INE renumera la estadistica experimental.
TABLE_HOUSING_MUNICIPALITIES = "39363"
TABLE_HOUSING_PROVINCES = "39364"

# Indicadores dentro de esas tablas, tal y como los nombra el INE.
HOUSING_INDICATORS = {
    "viviendas turisticas": "dwellings",
    "plazas": "beds",
    "plazas por vivienda turistica": "beds_per_dwelling",
}


def _normalize(text: str) -> str:
    import unicodedata

    limpio = unicodedata.normalize("NFKD", text or "")
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", limpio).strip().lower()


class IneTourismSource(BaseSource):
    """Estadísticas turísticas del INE por la misma API Tempus3 ya usada."""

    key = "ine_turismo"
    name = "INE · viviendas turísticas y ocupación"
    kind = "rental"
    required = False
    docs_url = "https://www.ine.es/experimental/viv_turistica/experimental_viv_turistica.htm"
    licence = "Datos abiertos. Reutilización libre citando al INE."

    # -- viviendas turísticas por municipio ---------------------------------

    def housing_table(self, table_id: str = TABLE_HOUSING_MUNICIPALITIES,
                      last_n: int = 1) -> list[dict[str, Any]]:
        url = f"{self.settings.ine_base_url}/DATOS_TABLA/{table_id}"
        response = self.request("GET", url, params={"nult": last_n})
        if response.status_code != 200:
            raise SourceError(f"INE Tempus3 HTTP {response.status_code} en la tabla {table_id}")
        payload = response.json()
        if not isinstance(payload, list):
            raise SourceError("El INE no devolvió una tabla: revisa el identificador.")
        return payload

    @staticmethod
    def parse_housing(payload: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Agrupa la respuesta por municipio.

        El INE mete el territorio y el indicador en el mismo campo `Nombre`,
        separados por puntos, así que hay que partirlo. Se hace por palabra
        clave y no por posición porque el orden de los trozos no es fijo.
        """
        por_territorio: dict[str, dict[str, Any]] = {}

        for serie in payload:
            partes = [p.strip() for p in str(serie.get("Nombre", "")).split(".") if p.strip()]
            if not partes:
                continue

            indicador = None
            territorio_partes: list[str] = []
            for parte in partes:
                clave = HOUSING_INDICATORS.get(_normalize(parte))
                if clave and indicador is None:
                    indicador = clave
                else:
                    territorio_partes.append(parte)
            if indicador is None:
                continue

            territorio = " ".join(territorio_partes).strip()
            if not territorio:
                continue

            # El dato más reciente de la serie es el que interesa.
            valores = [d for d in serie.get("Data", []) if d.get("Valor") is not None]
            if not valores:
                continue
            ultimo = max(valores, key=lambda d: (d.get("Anyo") or 0, d.get("FK_Periodo") or 0))

            registro = por_territorio.setdefault(
                territorio,
                {"name": territorio, "key": _normalize(territorio), "period": None},
            )
            registro[indicador] = float(ultimo["Valor"])
            registro["period"] = ultimo.get("Anyo")

        return por_territorio

    def tourist_housing(self, municipal: bool = True) -> dict[str, dict[str, Any]]:
        tabla = TABLE_HOUSING_MUNICIPALITIES if municipal else TABLE_HOUSING_PROVINCES
        return self.parse_housing(self.housing_table(tabla))

    # -- ocupación y tarifa en apartamentos turísticos ----------------------

    def find_tables(self, operation: str, *keywords: str) -> list[dict[str, Any]]:
        """Tablas de una operación cuyo título contenga todas las palabras.

        Los identificadores de la encuesta de ocupación no están publicados en
        ninguna parte estable, así que se buscan en tiempo de ejecución en vez
        de fijarlos a ciegas y descubrir en producción que ya no valen.
        """
        url = f"{self.settings.ine_base_url}/TABLAS_OPERACION/{operation}"
        response = self.request("GET", url)
        if response.status_code != 200:
            raise SourceError(f"INE Tempus3 HTTP {response.status_code} en {operation}")
        tablas = response.json()
        if not isinstance(tablas, list):
            return []

        buscadas = [_normalize(k) for k in keywords]
        return [
            t for t in tablas
            if all(palabra in _normalize(str(t.get("Nombre", ""))) for palabra in buscadas)
        ]

    def check(self) -> SourceStatus:
        return self._timed_probe(
            f"{self.settings.ine_base_url}/DATOS_TABLA/{TABLE_HOUSING_PROVINCES}",
            params={"nult": 1},
        )


def tourist_intensity(record: dict[str, Any]) -> float | None:
    """Plazas de alquiler turístico por vivienda del municipio.

    Es el indicador que separa un pueblo con demanda turística real de otro
    que sólo está cerca de la playa, y no depende de que nadie publique
    precios.
    """
    beds = record.get("beds")
    dwellings = record.get("dwellings")
    if not beds or not dwellings:
        return None
    return round(beds / dwellings, 2)
