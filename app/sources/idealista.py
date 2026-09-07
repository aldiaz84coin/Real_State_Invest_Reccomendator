"""Conector con la API oficial de Idealista.

Es la unica via legal de leer anuncios de un portal grande espanol:
Fotocasa solo ofrece API de alta de inmuebles (no de consulta) y pisos.com
no publica API alguna. La clave se solicita en developers.idealista.com.
"""
from __future__ import annotations

import base64
import time
from typing import Any, Iterable

import httpx

from app.sources.base import BaseSource, SourceError, SourceStatus

# La API acepta como mucho 50 resultados por pagina.
MAX_ITEMS_PER_PAGE = 50


class IdealistaSource(BaseSource):
    key = "idealista"
    name = "Idealista (API oficial)"
    kind = "listings"
    required = False
    docs_url = "https://developers.idealista.com/access-request"
    licence = "Comercial. Requiere clave nominal; plan de desarrollo ~100 llamadas/mes."

    def __init__(self) -> None:
        super().__init__()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- autenticacion ---------------------------------------------------

    def _basic_auth_header(self) -> str:
        raw = f"{self.settings.idealista_api_key}:{self.settings.idealista_api_secret}"
        return base64.b64encode(raw.encode()).decode()

    def get_token(self, force: bool = False) -> str:
        """Obtiene (y cachea) un token OAuth2 client_credentials."""
        if not self.settings.idealista_configured:
            raise SourceError(
                "Faltan IDEALISTA_API_KEY / IDEALISTA_API_SECRET. "
                "Solicitalas en developers.idealista.com."
            )
        if self._token and not force and time.time() < self._token_expires_at - 60:
            return self._token

        url = f"{self.settings.idealista_base_url}/oauth/token"
        headers = {
            "Authorization": f"Basic {self._basic_auth_header()}",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        }
        response = self.request(
            "POST",
            url,
            headers=headers,
            data={"grant_type": "client_credentials", "scope": "read"},
        )
        if response.status_code != 200:
            raise SourceError(
                f"Idealista rechazo las credenciales (HTTP {response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    # -- consulta --------------------------------------------------------

    def search_lands(
        self,
        lat: float,
        lon: float,
        radius_km: float = 20.0,
        *,
        min_size_m2: float | None = None,
        max_size_m2: float | None = None,
        max_price: float | None = None,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        """Busca terrenos en venta alrededor de un punto.

        Devuelve los anuncios ya normalizados al esquema interno.
        """
        token = self.get_token()
        url = f"{self.settings.idealista_base_url}/3.5/es/search"
        results: list[dict[str, Any]] = []

        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "operation": "sale",
                "propertyType": "lands",
                "center": f"{lat},{lon}",
                "distance": int(radius_km * 1000),
                "maxItems": MAX_ITEMS_PER_PAGE,
                "numPage": page,
                "order": "priceDown",
                "sort": "asc",
                "locale": "es",
            }
            if min_size_m2:
                params["minSize"] = int(min_size_m2)
            if max_size_m2:
                params["maxSize"] = int(max_size_m2)
            if max_price:
                params["maxPrice"] = int(max_price)

            response = self.request(
                "POST", url, headers={"Authorization": f"Bearer {token}"}, params=params
            )
            if response.status_code == 429:
                raise SourceError(
                    "Cuota de la API de Idealista agotada (HTTP 429). "
                    "El plan de desarrollo son ~100 llamadas al mes."
                )
            if response.status_code != 200:
                raise SourceError(
                    f"Busqueda fallida (HTTP {response.status_code}): {response.text[:200]}"
                )

            payload = response.json()
            batch = payload.get("elementList", [])
            results.extend(self.normalize(item) for item in batch)

            if page >= int(payload.get("totalPages", 1)) or not batch:
                break

        return results

    @staticmethod
    def normalize(item: dict[str, Any]) -> dict[str, Any]:
        """Traduce un elemento de Idealista al esquema interno de anuncio."""
        price = float(item.get("price") or 0)
        area = float(item.get("size") or 0)
        detailed = item.get("detailedType") or {}
        return {
            "source": "idealista",
            "external_id": str(item.get("propertyCode")),
            "url": item.get("url", ""),
            "title": item.get("suggestedTexts", {}).get("title", "") or item.get("address", ""),
            "description": item.get("description", "") or "",
            "price_eur": price,
            "area_m2": area,
            "price_eur_m2": round(price / area, 2) if area else 0.0,
            "lat": float(item.get("latitude") or 0),
            "lon": float(item.get("longitude") or 0),
            "address": item.get("address", "") or "",
            "municipality_name": item.get("municipality", "") or "",
            "province": item.get("province", "") or "",
            "land_type": detailed.get("subTypology") or item.get("propertyType", "lands"),
            "raw": item,
        }

    def check(self) -> SourceStatus:
        if not self.settings.idealista_configured:
            return self._status(
                "needs_credentials",
                "API oficial disponible pero sin credenciales configuradas. "
                "Solicita la clave en developers.idealista.com y rellena "
                "IDEALISTA_API_KEY / IDEALISTA_API_SECRET.",
            )
        started = time.perf_counter()
        try:
            self.get_token(force=True)
        except SourceError as exc:
            return self._status("needs_credentials", str(exc))
        except httpx.ProxyError as exc:
            return self._status("unavailable", f"Bloqueado por el proxy de salida: {exc}")
        except httpx.HTTPError as exc:
            return self._status("error", f"{type(exc).__name__}: {exc}")
        latency = int((time.perf_counter() - started) * 1000)
        return self._status("ok", "Token OAuth2 obtenido correctamente.", 200, latency)


class FotocasaSource(BaseSource):
    """Fotocasa no expone lectura publica; se declara para que quede explicito."""

    key = "fotocasa"
    name = "Fotocasa"
    kind = "listings"
    required = False
    docs_url = "https://pro.fotocasa.es/soluciones-fotocasa-pro-data/"
    licence = "Sin API publica de consulta."

    def check(self) -> SourceStatus:
        return self._status(
            "unavailable",
            "Su API es solo para que las inmobiliarias publiquen inmuebles, no para "
            "consultarlos. La lectura de mercado se vende como 'Fotocasa Pro Data' "
            "(cubre Fotocasa, Habitaclia y Milanuncios) mediante contrato comercial. "
            "No hay via oficial gratuita, asi que esta app no la usa.",
        )


class PisosComSource(BaseSource):
    """pisos.com tampoco publica API: se documenta el hueco, no se rodea."""

    key = "pisos_com"
    name = "pisos.com"
    kind = "listings"
    required = False
    docs_url = "https://www.pisos.com/"
    licence = "Sin API publica."

    def check(self) -> SourceStatus:
        return self._status(
            "unavailable",
            "No existe portal de desarrolladores ni API publica. Sus condiciones de "
            "uso prohiben la extraccion automatizada, de modo que esta app no lo "
            "consulta. Cobertura equivalente via Idealista + Catastro.",
        )


def iter_listing_sources() -> Iterable[BaseSource]:
    yield IdealistaSource()
    yield FotocasaSource()
    yield PisosComSource()
