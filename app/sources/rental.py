"""Datos reales de alquiler turistico para el plan de negocio (funcionalidad 4).

InsideAirbnb publica volcados CSV de anuncios reales de Airbnb con precio,
disponibilidad y resenas. Es gratuito y suficiente para estimar tarifa media
(ADR) y ocupacion por zona. AirDNA queda descartado: su API es solo enterprise
(del orden de 50.000 $/ano), asi que se ofrece AirROI como complemento opcional
de pago por uso.
"""
from __future__ import annotations

import csv
import gzip
import io
import re
import statistics
from typing import Any

from app.sources.base import BaseSource, SourceError, SourceStatus

# Proporcion de estancias que dejan resena. Es la hipotesis del "modelo de San
# Francisco" que usa el propio InsideAirbnb para estimar ocupacion.
REVIEW_RATE = 0.50
DEFAULT_STAY_NIGHTS = 4.5
MAX_PLAUSIBLE_OCCUPANCY = 0.85


class InsideAirbnbSource(BaseSource):
    key = "insideairbnb"
    name = "InsideAirbnb (anuncios reales de Airbnb)"
    kind = "rental"
    required = False
    docs_url = "https://insideairbnb.com/get-the-data"
    licence = "Dominio publico (CC0). Atribucion recomendada."

    def discover_spain_datasets(self) -> list[dict[str, str]]:
        """Localiza los volcados disponibles para ciudades espanolas."""
        response = self.request("GET", self.settings.insideairbnb_url)
        if response.status_code != 200:
            raise SourceError(f"InsideAirbnb HTTP {response.status_code}")

        pattern = re.compile(
            r"https://data\.insideairbnb\.com/spain/([^/]+)/([^/]+)/([\d-]+)/(?:visualisations|data)/listings\.csv(?:\.gz)?"
        )
        seen: dict[str, dict[str, str]] = {}
        for match in pattern.finditer(response.text):
            url, region, city, date = match.group(0), *match.groups()
            # Nos quedamos con el volcado mas reciente de cada ciudad.
            if city not in seen or date > seen[city]["date"]:
                seen[city] = {"region": region, "city": city, "date": date, "url": url}
        return sorted(seen.values(), key=lambda d: d["city"])

    def fetch_listings(self, url: str) -> list[dict[str, Any]]:
        """Descarga y parsea un CSV de anuncios (soporta .gz)."""
        response = self.request("GET", url)
        if response.status_code != 200:
            raise SourceError(f"InsideAirbnb HTTP {response.status_code} en {url}")

        content = response.content
        if url.endswith(".gz"):
            content = gzip.decompress(content)
        text = content.decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(text)))

    @staticmethod
    def aggregate(rows: list[dict[str, Any]], *, entire_home_only: bool = True) -> dict[str, Any]:
        """Resume un volcado en ADR y ocupacion utilizables en el plan de negocio.

        La ocupacion se estima por dos vias independientes y se toma la menor,
        que es la prudente: el calendario suele estar inflado por anfitriones
        que bloquean fechas, y las resenas subestiman en anuncios nuevos.
        """
        prices: list[float] = []
        occupancies: list[float] = []

        for row in rows:
            if entire_home_only and row.get("room_type") != "Entire home/apt":
                continue

            price = _parse_price(row.get("price"))
            if price is None or not (10 <= price <= 2000):
                continue
            prices.append(price)

            # Via 1: noches ocupadas inferidas de las resenas.
            reviews_per_month = _parse_float(row.get("reviews_per_month")) or 0.0
            min_nights = min(_parse_float(row.get("minimum_nights")) or 1.0, 30.0)
            stay = max(min_nights, DEFAULT_STAY_NIGHTS)
            nights_from_reviews = (reviews_per_month / REVIEW_RATE) * stay * 12.0

            # Via 2: hueco cerrado del calendario anual.
            availability = _parse_float(row.get("availability_365"))
            nights_from_calendar = (365.0 - availability) if availability is not None else None

            candidates = [n for n in (nights_from_reviews, nights_from_calendar) if n and n > 0]
            if candidates:
                occupancies.append(min(min(candidates) / 365.0, MAX_PLAUSIBLE_OCCUPANCY))

        if not prices:
            return {"adr_eur": 0.0, "occupancy_rate": 0.0, "sample_size": 0}

        quantiles = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [0, 0, 0]
        return {
            "adr_eur": round(statistics.median(prices), 2),
            "adr_p25_eur": round(quantiles[0], 2),
            "adr_p75_eur": round(quantiles[2], 2),
            "occupancy_rate": round(statistics.median(occupancies), 4) if occupancies else 0.0,
            "sample_size": len(prices),
        }

    def check(self) -> SourceStatus:
        return self._timed_probe(self.settings.insideairbnb_url)


class AirRoiSource(BaseSource):
    """Complemento opcional de pago por uso, alternativa realista a AirDNA."""

    key = "airroi"
    name = "AirROI (datos Airbnb, opcional)"
    kind = "rental"
    required = False
    docs_url = "https://www.airroi.com/airbnb-data"
    licence = "Comercial. Pago por uso; existe nivel gratuito limitado."

    def check(self) -> SourceStatus:
        if not self.settings.airroi_api_key:
            return self._status(
                "needs_credentials",
                "Opcional. Sin AIRROI_API_KEY la app usa InsideAirbnb, que es "
                "gratuito y cubre las principales ciudades espanolas.",
            )
        return self._timed_probe(
            f"{self.settings.airroi_base_url}/v1/market/summary",
            headers={"Authorization": f"Bearer {self.settings.airroi_api_key}"},
        )


class AirDnaSource(BaseSource):
    """Declarado para dejar constancia de por que no se usa."""

    key = "airdna"
    name = "AirDNA"
    kind = "rental"
    required = False
    docs_url = "https://apidocs.airdna.co/"
    licence = "Solo contrato enterprise."

    def check(self) -> SourceStatus:
        return self._status(
            "unavailable",
            "Su API solo se comercializa con contrato enterprise (del orden de "
            "50.000 $/ano), inviable para este proyecto. Se sustituye por "
            "InsideAirbnb (gratuito) y opcionalmente AirROI (pago por uso).",
        )


def _parse_price(value: Any) -> float | None:
    """InsideAirbnb publica el precio como '$1,234.00'."""
    if value in (None, "", "NA"):
        return None
    cleaned = re.sub(r"[^\d.,-]", "", str(value))
    if not cleaned:
        return None
    # Si hay coma y punto, la coma es separador de millares.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
