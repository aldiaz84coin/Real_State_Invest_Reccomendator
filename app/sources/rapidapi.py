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

import json
import time
from typing import Any, Iterable

import httpx

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
    # Varias rutas candidatas en vez de una sola: el esquema de estos
    # revendedores no esta documentado de forma fiable y la ruta correcta
    # varia entre proveedores. Se prueban en orden hasta que una devuelve
    # anuncios, y se recuerda cual funciono.
    search_paths: tuple[str, ...] = ("/",)
    search_method: str = "GET"
    portal: str = ""
    # Ruta barata para comprobar el estado sin consumir una busqueda. Solo
    # algunos proveedores la ofrecen.
    health_path: str | None = None

    def __init__(self) -> None:
        super().__init__()
        self.host = self.settings.rapidapi_host_for(self.key) or self.host
        override = self.settings.rapidapi_path_for(self.key)
        if override:
            self.search_paths = (override,)
        self._working_path: str | None = None
        # Anuncios recibidos que no se pudieron usar, por motivo. Se reporta
        # en la ingesta: descartar en silencio oculta un mapeo roto.
        self.discarded: dict[str, int] = {}

    @property
    def api_key(self) -> str:
        """RapidAPI da una clave por aplicacion y suele haber una por API."""
        return self.settings.rapidapi_key_for(self.key)

    @property
    def search_path(self) -> str:
        """Ruta en uso: la que ya funciono, o la primera candidata."""
        return self._working_path or self.search_paths[0]

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.host)

    def headers(self) -> dict[str, str]:
        return {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": self.host,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def base_url(self) -> str:
        return f"https://{self.host}"

    def build_params(
        self, lat: float, lon: float, radius_km: float, **filters: Any
    ) -> dict[str, Any]:  # pragma: no cover - lo concreta cada portal
        raise NotImplementedError

    def prepare_params(
        self, lat: float, lon: float, radius_km: float, **filters: Any
    ) -> dict[str, Any]:
        """Parametros listos para la peticion.

        Existe aparte de build_params porque algun proveedor necesita una
        llamada previa para resolver la zona antes de poder buscar.
        """
        return self.build_params(lat, lon, radius_km, **filters)

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
            params = self.prepare_params(
                lat, lon, radius_km,
                min_size_m2=min_size_m2, max_size_m2=max_size_m2,
                max_price=max_price, page=page,
            )
            path, payload = self.fetch_page(params)
            items = extract_listings(payload)
            if not items:
                break
            self._working_path = path
            for item in items:
                normalized = self.normalize(item)
                if normalized is None:
                    reason = self.discard_reason(item)
                    self.discarded[reason] = self.discarded.get(reason, 0) + 1
                    continue
                results.append(normalized)
        return results

    def discard_reason(self, item: dict[str, Any]) -> str:
        """Por qué no se pudo usar un anuncio. Lo consume el diagnóstico."""
        if _as_float(pick(item, FIELD_CANDIDATES["price_eur"])) in (None, 0):
            return "sin precio"
        if _as_float(pick(item, FIELD_CANDIDATES["area_m2"])) in (None, 0):
            return "sin superficie"
        if pick(item, FIELD_CANDIDATES["external_id"]) is None:
            return "sin identificador"
        return "motivo desconocido"

    def fetch_page(self, params: dict[str, Any]) -> tuple[str, Any]:
        """Pide una pagina probando las rutas candidatas.

        Devuelve la ruta que funciono y el cuerpo ya decodificado. Solo se
        pasa a la siguiente candidata cuando la respuesta indica que la ruta
        no existe; un 401 o un 429 son problemas de la clave o de la cuota y
        no mejoran cambiando de ruta.
        """
        candidates = (self._working_path,) if self._working_path else self.search_paths
        errors: list[str] = []

        for path in candidates:
            response = self.request(
                self.search_method,
                f"{self.base_url()}{path}",
                headers=self.headers(),
                params=params,
            )
            if response.status_code == 429:
                raise SourceError(f"{self.name}: cuota de RapidAPI agotada (HTTP 429).")
            if response.status_code in (401, 403):
                raise SourceError(
                    f"{self.name}: clave rechazada (HTTP {response.status_code}). "
                    "En RapidAPI hay que suscribirse a CADA API por separado, "
                    "incluso al plan gratuito: estar suscrito a otra no sirve. "
                    f"Entra en {self.docs_url}, pulsa «Subscribe to Test» y elige "
                    "el plan Basic."
                )
            if response.status_code in (404, 400):
                errors.append(f"{path} -> HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise SourceError(
                    f"{self.name}: HTTP {response.status_code} en {path} — "
                    f"{response.text[:180]}"
                )
            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{path} -> respuesta no JSON")
                continue
            # El proveedor devuelve 200 con cuerpo de error en algunos fallos
            # de la fuente de origen; conviene no tomarlo por resultado vacío.
            if isinstance(payload, dict) and payload.get("error"):
                raise SourceError(
                    f"{self.name}: {payload.get('error')} — "
                    f"{payload.get('message', 'sin detalle')}"
                )
            return path, payload

        raise SourceError(
            f"{self.name}: ninguna ruta candidata respondió. Intentos: "
            + "; ".join(errors)
            + ". Ajusta RAPIDAPI_PATHS con la ruta correcta."
        )

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

        # Sin precio, superficie o identificador el anuncio no sirve de nada.
        if not price or not area or external_id is None:
            return None

        # Las coordenadas sí pueden faltar: la busqueda de algunos proveedores
        # no las devuelve y solo aparecen en el detalle de cada anuncio, que
        # costaria una peticion extra por inmueble. En vez de descartarlos, se
        # marcan como pendientes y la ingesta los situa en el municipio.
        raw = dict(item)
        raw["coords_precision"] = "exact" if lat is not None and lon is not None else "missing"

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
            "coords_precision": raw["coords_precision"],
            "address": str(pick(item, FIELD_CANDIDATES["address"]) or ""),
            "municipality_name": str(pick(item, FIELD_CANDIDATES["municipality_name"]) or ""),
            "province": str(pick(item, FIELD_CANDIDATES["province"]) or ""),
            "land_type": str(pick(item, FIELD_CANDIDATES["land_type"]) or "lands"),
            "raw": raw,
        }

    def check(self) -> SourceStatus:
        if not self.api_key:
            return self._status(
                "needs_credentials",
                "Respaldo no oficial, desactivado. Configura RAPIDAPI_KEY (o una "
                f"clave propia en RAPIDAPI_KEYS como '{self.key}=...') para "
                f"habilitarlo. Cubre {self.portal or 'el portal'} cuando la vía "
                "oficial no está disponible.",
            )
        if not self.host:
            return self._status(
                "needs_credentials",
                f"Falta el host de RapidAPI para {self.key}. Defínelo en "
                f"RAPIDAPI_HOSTS como '{self.key}=mi-host.p.rapidapi.com'.",
            )
        cached = _cached_status(self.key)
        if cached is not None:
            return cached

        # Si el proveedor ofrece un endpoint de salud, se usa ese: comprobar
        # con una busqueda real gastaria cuota del plan gratuito en cada carga
        # del panel.
        if self.health_path:
            status = self._timed_probe(
                f"{self.base_url()}{self.health_path}", headers=self.headers()
            )
            if status.access == "ok":
                status.detail = (
                    f"El proveedor responde en {self.health_path}. El mapeo de "
                    "anuncios se verifica en /api/sources/rapidapi/probe, que sí "
                    "consume una petición."
                )
            _store_status(self.key, status)
            return status

        # Sin endpoint de salud se sondea con el mismo mecanismo que la
        # búsqueda real, probando las rutas candidatas: sondear sólo la primera
        # marcaba en rojo a proveedores que sí funcionan por otra ruta.
        started = time.perf_counter()
        try:
            # prepare_params y no build_params: los proveedores que resuelven
            # la zona en una llamada previa necesitan ese paso, y saltárselo
            # manda la petición sin parámetros obligatorios.
            path, payload = self.fetch_page(
                self.prepare_params(36.7213, -4.4214, 10.0, page=1)
            )
        except SourceError as exc:
            message = str(exc)
            latency = int((time.perf_counter() - started) * 1000)
            if "rechazada" in message:
                return _store_status(self.key, self._status("needs_credentials", message, 401, latency))
            if "cuota" in message:
                return _store_status(self.key, self._status("error", message, 429, latency))
            return _store_status(self.key, self._status("error", message, latency_ms=latency))
        except httpx.ProxyError as exc:
            return self._status("unavailable", f"Bloqueado por el proxy de salida: {exc}")
        except httpx.HTTPError as exc:
            return self._status("error", f"{type(exc).__name__}: {exc}")

        latency = int((time.perf_counter() - started) * 1000)
        found = len(extract_listings(payload))
        detail = f"Responde en {path}. Anuncios localizados en la prueba: {found}."
        if not found:
            detail += (
                " Ninguno: puede ser normal si no hay terrenos en la zona de "
                "prueba, o indicar que hay que ajustar el mapeo. Compruébalo "
                "en /api/sources/rapidapi/probe."
            )
        return _store_status(self.key, self._status("ok", detail, 200, latency))


class RapidApiIdealistaSource(RapidApiSource):
    key = "rapidapi_idealista"
    name = "Idealista vía RapidAPI (respaldo no oficial)"
    portal = "Idealista"
    host = "idealista-api1.p.rapidapi.com"
    search_paths = ("/properties/list", "/properties/search", "/property/search", "/search")
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
    """Cubre el hueco que Fotocasa no permite por vía oficial.

    Su API no busca por radio sino por zona: /searchads exige un
    `combinedLocations`, un identificador que hay que pedir antes a
    /suggestions con el nombre del municipio. Por eso la busqueda aqui son dos
    pasos, y el municipio se deduce del punto por geocodificacion inversa para
    no pedirselo al usuario.
    """

    key = "rapidapi_fotocasa"
    name = "Fotocasa vía RapidAPI (respaldo no oficial)"
    portal = "Fotocasa"
    host = "fotocasa3.p.rapidapi.com"
    search_paths = ("/searchads",)
    suggestions_path = "/suggestions"
    health_path = "/health"
    docs_url = "https://rapidapi.com/happyendpoint/api/fotocasa3"

    def __init__(self) -> None:
        super().__init__()
        self._locations_cache: dict[str, str] = {}
        self.last_location_choice: dict[str, Any] = {}

    def build_params(self, lat: float, lon: float, radius_km: float, **filters: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "pageNumber": filters.get("page", 1),
            "size": 30,
            "transactionType": "BUY",
            "propertyType": "LAND",          # terrenos, que es lo que busca la app
            "publicationDate": "INDIFFERENT",
            "sortType": "PRICE_PER_AREA",    # los mas baratos por m2 primero
            "sortOrderDesc": "false",
        }
        combined = filters.get("combined_locations")
        if combined:
            params["combinedLocations"] = combined
        if filters.get("min_size_m2"):
            params["minSurface"] = int(filters["min_size_m2"])
        if filters.get("max_size_m2"):
            params["maxSurface"] = int(filters["max_size_m2"])
        if filters.get("max_price"):
            params["maxPrice"] = int(filters["max_price"])
        return params

    def prepare_params(self, lat: float, lon: float, radius_km: float, **filters: Any) -> dict[str, Any]:
        if not filters.get("combined_locations"):
            filters["combined_locations"] = self.resolve_location(lat, lon)
        return self.build_params(lat, lon, radius_km, **filters)

    def resolve_location(self, lat: float, lon: float, query: str | None = None) -> str:
        """Traduce un punto al identificador de zona que exige /searchads."""
        if query is None:
            # Nominatim es gratuito y no consume cuota de RapidAPI.
            from app.sources.osm import NominatimSource

            place = NominatimSource().reverse(lat, lon)
            if not place:
                raise SourceError(
                    f"{self.name}: no se pudo determinar el municipio de "
                    f"({lat}, {lon}), y su API busca por zona, no por radio."
                )
            query = place["municipality"]

        # La cache incluye el punto: la eleccion depende de las coordenadas,
        # no solo del nombre.
        cache_key = f"{query}|{lat:.3f},{lon:.3f}"
        if cache_key in self._locations_cache:
            return self._locations_cache[cache_key]

        response = self.request(
            "GET",
            f"{self.base_url()}{self.suggestions_path}",
            headers=self.headers(),
            params={"query": query},
        )
        if response.status_code != 200:
            raise SourceError(
                f"{self.name}: /suggestions devolvió HTTP {response.status_code} "
                f"para «{query}»."
            )
        try:
            payload = response.json()
        except ValueError:
            raise SourceError(f"{self.name}: /suggestions no devolvió JSON.") from None

        combined, detalle = select_location_id(payload, lat, lon)
        self.last_location_choice = detalle
        if not combined:
            # El cuerpo va en el propio error: sin el no hay forma de saber
            # como llama este proveedor al identificador de zona, y remitir al
            # diagnostico seria circular porque falla en este mismo punto.
            cuerpo = json.dumps(payload, ensure_ascii=False)[:800]
            raise SourceError(
                f"{self.name}: /suggestions respondió pero ninguna sugerencia "
                f"para «{query}» cae a menos de {MAX_SUGGESTION_DISTANCE_KM:.0f} km "
                f"del punto buscado. Respuesta recibida: {cuerpo}"
            )
        self._locations_cache[cache_key] = str(combined)
        return str(combined)

    def suggestions_raw(self, query: str) -> dict[str, Any]:
        """Respuesta cruda de /suggestions, para diagnosticar el mapeo."""
        response = self.request(
            "GET",
            f"{self.base_url()}{self.suggestions_path}",
            headers=self.headers(),
            params={"query": query},
        )
        result: dict[str, Any] = {
            "query": query,
            "path": self.suggestions_path,
            "http_status": response.status_code,
        }
        try:
            payload = response.json()
        except ValueError:
            result["body_preview"] = response.text[:1500]
            result["error"] = "La respuesta no es JSON."
            return result

        result["body"] = payload
        result["combined_locations_found"] = _find_combined_locations(payload)
        return result

    def suggestions_choice(self, query: str, lat: float, lon: float) -> dict[str, Any]:
        """Qué sugerencia se elegiría para un punto, y por qué."""
        raw = self.suggestions_raw(query)
        identifier, detalle = select_location_id(raw.get("body"), lat, lon)
        return {**raw, "selected": identifier, "selection": detalle}


class RapidApiIdealista17Source(RapidApiSource):
    """Idealista Data API de HappyEndpoint.

    Su esquema replica el de la API oficial de Idealista (envoltorio
    `elementList`, `propertyCode`, `price`, `size`), asi que el mapeo generico
    ya encaja. Se usa la busqueda por coordenadas porque es la unica que casa
    con como busca esta aplicacion: un radio alrededor de un punto.

    Ojo con la cuota: el plan gratuito son 500 peticiones al mes, de ahi que
    el numero de paginas por defecto sea bajo.
    """

    key = "rapidapi_idealista17"
    name = "Idealista Data API vía RapidAPI (respaldo no oficial)"
    portal = "Idealista"
    host = "idealista17.p.rapidapi.com"
    # Rutas reales de su documentacion, de la mas especifica a la mas general.
    search_paths = ("/property-search-by-coordinates", "/property-search")
    docs_url = "https://rapidapi.com/happyendpoint/api/idealista17"
    licence = (
        "No oficial. Servicio independiente sin relación con Idealista; "
        "plan gratuito de 500 peticiones al mes."
    )

    def build_params(self, lat: float, lon: float, radius_km: float, **filters: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "country": "es",
            "operation": "sale",
            "propertyType": "lands",
            "locale": "es",
            "latitude": lat,
            "longitude": lon,
            "radius": int(radius_km * 1000),
            "page": filters.get("page", 1),
            "maxItems": 40,
        }
        if filters.get("min_size_m2"):
            params["minSize"] = int(filters["min_size_m2"])
        if filters.get("max_size_m2"):
            params["maxSize"] = int(filters["max_size_m2"])
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


# Nombres con los que un proveedor puede llamar al identificador de zona. El
# primero es el real de Fotocasa; los demas cubren variaciones.
COMBINED_LOCATION_KEYS = (
    "combinedlocationids",
    "combinedlocations",
    "combinedlocation",
    "combined",
    "locationids",
    "locationid",
)

# Distancia maxima a la que una sugerencia puede considerarse del sitio
# buscado. /suggestions responde por coincidencia de texto, asi que al pedir
# "Malaga" devuelve tambien un Malaga de Grinon y otro de Villaviciosa de
# Odon, ambos en Madrid y a mas de 400 km.
MAX_SUGGESTION_DISTANCE_KM = 30.0

# Un identificador tiene un segmento por nivel administrativo. El municipio
# suele quedar en el sexto: menos segmentos es la provincia entera y mas es un
# barrio, demasiado estrecho para una busqueda por zona.
TARGET_LOCATION_DEPTH = 6


def _location_depth(identifier: Any) -> int:
    """Cuantos niveles concreta un identificador de zona."""
    parts = str(identifier).split(",")
    if not all(part.strip().lstrip("-").isdigit() for part in parts):
        return 0  # identificadores con forma de slug, no jerarquicos
    return sum(1 for part in parts if part.strip() not in ("0", ""))


def _iter_suggestions(payload: Any) -> Iterable[dict[str, Any]]:
    """Recorre los diccionarios de la respuesta que llevan identificador."""
    if isinstance(payload, dict):
        if _find_combined_locations(payload, deep=False):
            yield payload
        for value in payload.values():
            yield from _iter_suggestions(value)
    elif isinstance(payload, list):
        for element in payload:
            yield from _iter_suggestions(element)


def _find_combined_locations(payload: Any, deep: bool = True) -> Any:
    for name in COMBINED_LOCATION_KEYS:
        found = _find_key(payload, name) if deep else _shallow_key(payload, name)
        if found:
            return found
    return None


def _shallow_key(payload: Any, target: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if str(key).lower() == target and value not in (None, "", [], {}):
            return value
    return None


def select_location_id(payload: Any, lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
    """Elige la sugerencia que de verdad corresponde al punto buscado.

    No vale con tomar la primera: /suggestions ordena por relevancia de texto y
    la primera suele ser la provincia entera, ademas de colarse municipios
    homonimos de otra punta del pais. Se filtra por distancia real a las
    coordenadas y, entre las cercanas, se prefiere el nivel de municipio.
    """
    from app.analysis.geo import haversine_km

    candidatos: list[tuple[float, float, Any, dict[str, Any]]] = []
    for suggestion in _iter_suggestions(payload):
        identifier = _find_combined_locations(suggestion, deep=False)
        coordinates = suggestion.get("coordinates") or {}
        s_lat = _as_float(coordinates.get("latitude"))
        s_lon = _as_float(coordinates.get("longitude"))
        if s_lat is None or s_lon is None:
            continue
        distance = haversine_km(lat, lon, s_lat, s_lon)
        if distance > MAX_SUGGESTION_DISTANCE_KM:
            continue
        depth_gap = abs(_location_depth(identifier) - TARGET_LOCATION_DEPTH)
        candidatos.append((depth_gap, distance, identifier, suggestion))

    if not candidatos:
        return None, {}
    candidatos.sort(key=lambda item: (item[0], item[1]))
    _, distance, identifier, suggestion = candidatos[0]
    return identifier, {
        "text": suggestion.get("text", ""),
        "distance_km": round(distance, 2),
        "depth": _location_depth(identifier),
        "candidates_considered": len(candidatos),
    }


def _find_key(payload: Any, target: str) -> Any:
    """Busca en profundidad el primer valor de una clave, sin importar dónde
    la anide el proveedor ni cómo la capitalice."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() == target and value not in (None, "", [], {}):
                return value
        for value in payload.values():
            found = _find_key(value, target)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for element in payload:
            found = _find_key(element, target)
            if found is not None:
                return found
    return None


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


# Cada comprobacion contra RapidAPI cuesta una peticion del plan contratado, y
# el panel sondea todas las fuentes en cada carga. Sin cache, unas pocas
# visitas al dia agotarian un plan gratuito de 500 al mes solo comprobando.
CHECK_CACHE_SECONDS = 900
_check_cache: dict[str, tuple[float, SourceStatus]] = {}


def _cached_status(key: str) -> SourceStatus | None:
    entry = _check_cache.get(key)
    if entry is None:
        return None
    stored_at, status = entry
    if time.time() - stored_at > CHECK_CACHE_SECONDS:
        _check_cache.pop(key, None)
        return None
    return status


def _store_status(key: str, status: SourceStatus) -> SourceStatus:
    _check_cache[key] = (time.time(), status)
    return status


def clear_check_cache() -> None:
    """Fuerza una comprobacion nueva. La usa el endpoint de diagnostico."""
    _check_cache.clear()


def iter_rapidapi_sources() -> Iterable[RapidApiSource]:
    # idealista17 va primero: es el proveedor mas completo de los tres.
    yield RapidApiIdealista17Source()
    yield RapidApiIdealistaSource()
    yield RapidApiFotocasaSource()
