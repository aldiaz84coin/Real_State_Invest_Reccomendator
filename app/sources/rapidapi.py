"""Fuentes de respaldo servidas a traves de RapidAPI.

Aviso importante: estas APIs *no* son oficiales. Son revendedores que extraen
los datos de los portales, asi que quedan fuera del criterio de "solo vias
oficiales" con el que se construyo el resto de la aplicacion. Se integran como
respaldo explicito, desactivadas mientras no haya clave, y nunca desplazan a la
API oficial de Idealista cuando esta esta configurada.

Sus condiciones y su fiabilidad dependen de cada proveedor: cuando el portal
cambia su web, estos adaptadores se rompen. Por eso el mapeo de campos es
configurable y el codigo no asume una forma de respuesta concreta.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.sources.base import BaseSource, SourceError, SourceStatus

# Nombres de campo que usan los distintos proveedores para lo mismo. Se prueban
# en orden y admiten rutas anidadas con puntos.
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "external_id": ("propertyCode", "id", "listingId", "code", "reference", "adId"),
    "url": ("url", "link", "detailUrl", "permalink", "shareUrl"),
    "price_eur": ("price", "priceAmount", "amount", "priceInfo.amount", "price.value"),
    "area_m2": ("size", "area", "surface", "constructedArea", "sizeM2", "m2", "surfaceArea"),
    "lat": ("latitude", "lat", "coordinates.latitude", "coordinates.lat", "location.latitude"),
    "lon": ("longitude", "lon", "lng", "coordinates.longitude", "coordinates.lon",
            "location.longitude"),
    "address": ("address", "addressText", "addressVisibility", "street", "locationName"),
    "municipality_name": ("municipality", "city", "town", "locality", "municipalityName"),
    "province": ("province", "region", "state", "provinceName"),
    "title": ("title", "name", "suggestedTexts.title", "headline"),
    "description": ("description", "summary", "comment", "detail"),
    "land_type": ("detailedType.subTypology", "propertyType", "typology", "subTypology"),
}

# Claves cuya presencia delata que un diccionario es un anuncio y no metadatos.
PRICE_HINTS = ("price", "priceamount", "amount", "priceinfo")


class RapidApiSource(BaseSource):
    """Adaptador generico. Cada portal concreta host, ruta y parametros."""

    kind = "listings"
    required = False
    licence = "No oficial. Revendedor de datos vía RapidAPI; revisa sus condiciones."

    host: str = ""
    search_path: str = "/"
    search_method: str = "GET"
    portal: str = ""

    def __init__(self) -> None:
        super().__init__()
        self.host = self.settings.rapidapi_host_for(self.key) or self.host
        self.search_path = self.settings.rapidapi_path_for(self.key) or self.search_path

    @property
    def configured(self) -> bool:
        return bool(self.settings.rapidapi_key and self.host)

    def headers(self) -> dict[str, str]:
        return {
            "x-rapidapi-key": self.settings.rapidapi_key,
            "x-rapidapi-host": self.host,
            "Accept": "application/json",
        }

    def base_url(self) -> str:
        return f"https://{self.host}"

    def build_params(
        self, lat: float, lon: float, radius_km: float, **filters: Any
    ) -> dict[str, Any]:  # pragma: no cover - lo concreta cada portal
        raise NotImplementedError

    def search_lands(
        self,
        lat: float,
        lon: float,
        radius_km: float = 20.0,
        *,
        min_size_m2: float | None = None,
        max_size_m2: float | None = None,
        max_price: float | None = None,
        max_pages: int = 1,
    ) -> list[dict[str, Any]]:
        """Busca terrenos y devuelve anuncios ya normalizados."""
        if not self.configured:
            raise SourceError(
                f"{self.name} necesita RAPIDAPI_KEY (y un host válido) para funcionar."
            )

        results: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params = self.build_params(
                lat, lon, radius_km,
                min_size_m2=min_size_m2, max_size_m2=max_size_m2,
                max_price=max_price, page=page,
            )
            response = self.request(
                self.search_method,
                f"{self.base_url()}{self.search_path}",
                headers=self.headers(),
                params=params,
            )
            if response.status_code == 429:
                raise SourceError(f"{self.name}: cuota de RapidAPI agotada (HTTP 429).")
            if response.status_code != 200:
                raise SourceError(
                    f"{self.name}: HTTP {response.status_code} — {response.text[:180]}"
                )

            try:
                payload = response.json()
            except ValueError:
                raise SourceError(f"{self.name} devolvió una respuesta que no es JSON.") from None

            items = extract_listings(payload)
            if not items:
                break
            results.extend(
                normalized for item in items
                if (normalized := self.normalize(item)) is not None
            )
        return results

    def normalize(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Traduce un anuncio del proveedor al esquema interno.

        Devuelve None si faltan los datos imprescindibles: sin precio,
        superficie o coordenadas el anuncio no sirve para el análisis.
        """
        price = _as_float(pick(item, FIELD_CANDIDATES["price_eur"]))
        area = _as_float(pick(item, FIELD_CANDIDATES["area_m2"]))
        lat = _as_float(pick(item, FIELD_CANDIDATES["lat"]))
        lon = _as_float(pick(item, FIELD_CANDIDATES["lon"]))
        external_id = pick(item, FIELD_CANDIDATES["external_id"])

        if not price or not area or lat is None or lon is None or external_id is None:
            return None

        return {
            "source": self.key,
            "external_id": str(external_id),
            "url": str(pick(item, FIELD_CANDIDATES["url"]) or ""),
            "title": str(pick(item, FIELD_CANDIDATES["title"]) or ""),
            "description": str(pick(item, FIELD_CANDIDATES["description"]) or ""),
            "price_eur": price,
            "area_m2": area,
            "price_eur_m2": round(price / area, 2),
            "lat": lat,
            "lon": lon,
            "address": str(pick(item, FIELD_CANDIDATES["address"]) or ""),
            "municipality_name": str(pick(item, FIELD_CANDIDATES["municipality_name"]) or ""),
            "province": str(pick(item, FIELD_CANDIDATES["province"]) or ""),
            "land_type": str(pick(item, FIELD_CANDIDATES["land_type"]) or "lands"),
            "raw": item,
        }

    def check(self) -> SourceStatus:
        if not self.settings.rapidapi_key:
            return self._status(
                "needs_credentials",
                "Respaldo no oficial, desactivado. Configura RAPIDAPI_KEY para "
                f"habilitarlo. Cubre {self.portal or 'el portal'} cuando la vía "
                "oficial no está disponible.",
            )
        if not self.host:
            return self._status(
                "needs_credentials",
                f"Falta el host de RapidAPI para {self.key}. Defínelo en "
                f"RAPIDAPI_HOSTS como '{self.key}=mi-host.p.rapidapi.com'.",
            )
        # Se sondea la propia ruta de búsqueda: es la que se va a usar, y un
        # 200 en la portada del proveedor no garantizaría que funcione.
        return self._timed_probe(
            f"{self.base_url()}{self.search_path}",
            method=self.search_method,
            headers=self.headers(),
            params=self.build_params(36.7213, -4.4214, 10.0, page=1),
            expect=(200, 204),
        )


class RapidApiIdealistaSource(RapidApiSource):
    key = "rapidapi_idealista"
    name = "Idealista vía RapidAPI (respaldo no oficial)"
    portal = "Idealista"
    host = "idealista-api1.p.rapidapi.com"
    search_path = "/properties/list"
    docs_url = "https://rapidapi.com/oneapiproject/api/idealista-api1"

    def build_params(self, lat: float, lon: float, radius_km: float, **filters: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "country": "es",
            "operation": "sale",
            "propertyType": "lands",
            "locale": "es",
            "center": f"{lat},{lon}",
            "latitude": lat,
            "longitude": lon,
            "radius": int(radius_km * 1000),
            "numPage": filters.get("page", 1),
            "maxItems": 40,
        }
        if filters.get("min_size_m2"):
            params["minSize"] = int(filters["min_size_m2"])
        if filters.get("max_size_m2"):
            params["maxSize"] = int(filters["max_size_m2"])
        if filters.get("max_price"):
            params["maxPrice"] = int(filters["max_price"])
        return params


class RapidApiFotocasaSource(RapidApiSource):
    """Cubre el hueco que Fotocasa no permite por vía oficial."""

    key = "rapidapi_fotocasa"
    name = "Fotocasa vía RapidAPI (respaldo no oficial)"
    portal = "Fotocasa"
    host = "fotocasa3.p.rapidapi.com"
    search_path = "/search"
    docs_url = "https://rapidapi.com/happyendpoint/api/fotocasa3"

    def build_params(self, lat: float, lon: float, radius_km: float, **filters: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "operation": "buy",
            "propertyType": "land",
            "latitude": lat,
            "longitude": lon,
            "distance": int(radius_km * 1000),
            "page": filters.get("page", 1),
        }
        if filters.get("max_price"):
            params["maxPrice"] = int(filters["max_price"])
        return params


def extract_listings(payload: Any) -> list[dict[str, Any]]:
    """Localiza la lista de anuncios dentro de una respuesta desconocida.

    Cada proveedor de RapidAPI envuelve los resultados a su manera
    (elementList, data, results, items...), y el esquema exacto no es
    verificable sin la clave. En vez de fijar una ruta, se busca la primera
    lista cuyos elementos parezcan anuncios: diccionarios con algo de precio.
    """
    if isinstance(payload, list):
        if _looks_like_listings(payload):
            return [item for item in payload if isinstance(item, dict)]
        for element in payload:
            found = extract_listings(element)
            if found:
                return found
        return []

    if isinstance(payload, dict):
        # Primero las claves habituales, para no bajar por ramas irrelevantes.
        for key in ("elementList", "data", "results", "items", "listings", "properties",
                    "hits", "content", "realEstates"):
            if key in payload:
                found = extract_listings(payload[key])
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (list, dict)):
                found = extract_listings(value)
                if found:
                    return found
    return []


def _looks_like_listings(candidate: list[Any]) -> bool:
    dicts = [item for item in candidate if isinstance(item, dict)]
    if not dicts:
        return False
    for item in dicts[:5]:
        lowered = {str(k).lower() for k in item}
        if any(hint in key for key in lowered for hint in PRICE_HINTS):
            return True
    return False


def pick(item: dict[str, Any], candidates: Iterable[str]) -> Any:
    """Primer valor no vacío entre los nombres candidatos, con rutas anidadas."""
    for path in candidates:
        value = _dig(item, path)
        if value not in (None, "", [], {}):
            return value
    return None


def _dig(item: Any, path: str) -> Any:
    current = item
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("€", "").replace(" ", "").replace("\u00a0", "")
    if not text:
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        # Con ambos separadores, el ultimo es el decimal. Asi funcionan tanto
        # "1.234,56" (español) como "1,234.56" (inglés) sin suponer el idioma.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        text = _resolve_separator(text, ",")
    elif has_dot:
        text = _resolve_separator(text, ".")

    try:
        return float(text)
    except ValueError:
        return None


def _resolve_separator(text: str, separator: str) -> str:
    """Decide si un separador suelto son millares o decimales.

    "1.250" son mil doscientos cincuenta en España y uno coma veinticinco en
    inglés: el texto por si solo es ambiguo. Se resuelve por el tamaño del
    ultimo grupo, que es la convencion habitual: tres digitos indican
    millares. Para precios y superficies, que es lo unico que se parsea aqui,
    esa lectura acierta salvo en valores por debajo de la unidad, que no se
    dan en euros ni en metros cuadrados de parcela.
    """
    if text.count(separator) > 1:
        return text.replace(separator, "")  # 1.234.567 solo puede ser millares

    head, _, tail = text.partition(separator)
    if head.lstrip("-").isdigit() and tail.isdigit() and len(tail) == 3:
        return head + tail
    return text.replace(separator, ".")


def iter_rapidapi_sources() -> Iterable[RapidApiSource]:
    yield RapidApiIdealistaSource()
    yield RapidApiFotocasaSource()
