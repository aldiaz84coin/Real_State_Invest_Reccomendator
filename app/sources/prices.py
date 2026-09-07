"""Series oficiales de precio: INE y Ministerio de Vivienda (MIVAU).

Son las fuentes que sustentan la funcionalidad 1 ("tendencia ascendente del
valor del suelo") y la 2 ("precio muy por debajo de la media de la zona"),
porque dan el historico por municipio que ningun portal publica.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from app.sources.base import BaseSource, SourceError, SourceStatus

# Indice de Precios de Vivienda (IPV), base 2015. Tabla del sistema Tempus3.
# El identificador se puede sobreescribir sin tocar codigo.
INE_IPV_TABLE = "25171"


class IneSource(BaseSource):
    """API Tempus3 del INE: JSON abierto, sin clave, sin cuota publicada."""

    key = "ine"
    name = "INE (API Tempus3)"
    kind = "prices"
    required = True
    docs_url = "https://www.ine.es/dyngs/DAB/index.htm?cid=1099"
    licence = "Datos abiertos. Reutilizacion libre citando al INE."

    def series_table(self, table_id: str = INE_IPV_TABLE, last_n: int = 20) -> list[dict[str, Any]]:
        """Descarga los ultimos `last_n` periodos de una tabla del INE."""
        url = f"{self.settings.ine_base_url}/DATOS_TABLA/{table_id}"
        with self.client() as client:
            response = client.get(url, params={"nult": last_n})
        if response.status_code != 200:
            raise SourceError(f"INE Tempus3 HTTP {response.status_code}")
        return response.json()

    @staticmethod
    def to_price_points(payload: list[dict[str, Any]]) -> list[tuple[int, int, float]]:
        """Aplana la respuesta del INE a tuplas (anio, trimestre, valor)."""
        points: list[tuple[int, int, float]] = []
        for series in payload:
            for entry in series.get("Data", []):
                value = entry.get("Valor")
                year = entry.get("Anyo")
                if value is None or year is None:
                    continue
                period = str(entry.get("T3_Periodo", "") or "")
                quarter = int(period[1]) if period.startswith("T") and period[1:2].isdigit() else 0
                points.append((int(year), quarter, float(value)))
        return sorted(points)

    def check(self) -> SourceStatus:
        return self._timed_probe(
            f"{self.settings.ine_base_url}/DATOS_TABLA/{INE_IPV_TABLE}", params={"nult": 1}
        )


class MivauLandPriceSource(BaseSource):
    """Estadistica de precio del suelo y valor tasado del Ministerio de Vivienda.

    El Ministerio publica los datos como descarga (CSV/XLSX), no como API REST,
    de modo que el conector expone un ingestor de fichero ademas del sondeo.
    """

    key = "mivau"
    name = "Ministerio de Vivienda (precio de suelo y vivienda)"
    kind = "prices"
    required = False
    docs_url = "https://www.mivau.gob.es/el-ministerio/observatorios-y-estadisticas/estadisticas"
    licence = "Datos abiertos. Reutilizacion libre citando la fuente."

    def check(self) -> SourceStatus:
        return self._timed_probe(self.docs_url)

    @staticmethod
    def parse_csv(
        content: str,
        *,
        municipality_column: str = "municipio",
        year_column: str = "anio",
        value_column: str = "eur_m2",
    ) -> list[dict[str, Any]]:
        """Lee un CSV de serie de precios a la estructura interna.

        Acepta separador ';' o ',' y coma decimal, que es como el Ministerio
        y el INE publican habitualmente sus descargas.
        """
        sample = content[:4096]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

        rows: list[dict[str, Any]] = []
        for row in reader:
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            municipality = normalized.get(municipality_column)
            year_raw = normalized.get(year_column)
            value_raw = normalized.get(value_column)
            if not municipality or not year_raw or not value_raw:
                continue
            try:
                year = int(str(year_raw)[:4])
                value = float(value_raw.replace(".", "").replace(",", "."))
            except ValueError:
                continue
            if value <= 0:
                continue
            rows.append({"municipality": municipality, "year": year, "eur_m2": value})
        return rows
