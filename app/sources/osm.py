"""OpenStreetMap: Overpass para puntos de interes y Nominatim para geocodificar.

Aporta la parte de "cerca de algun lugar de interes, zona turistica, playa o
montana" de la funcionalidad 1.
"""
from __future__ import annotations

from typing import Any

from app.sources.base import BaseSource, SourceError, SourceStatus

# Cada consulta pide a Overpass las entidades relevantes en un radio dado.
POI_QUERIES: dict[str, str] = {
    "beach": '(node["natural"="beach"]{bbox};way["natural"="beach"]{bbox};)',
    "mountain": '(node["natural"="peak"]{bbox};node["natural"="volcano"]{bbox};)',
    "tourism": (
        '(node["tourism"~"attraction|theme_park|museum|viewpoint|resort"]{bbox};'
        'way["tourism"~"attraction|theme_park|museum|resort"]{bbox};)'
    ),
    "heritage": (
        '(node["historic"~"castle|monument|ruins|archaeological_site"]{bbox};'
        'way["historic"~"castle|monument|ruins"]{bbox};)'
    ),
    "ski": '(node["landuse"="winter_sports"]{bbox};way["landuse"="winter_sports"]{bbox};)',
    "marina": '(node["leisure"="marina"]{bbox};way["leisure"="marina"]{bbox};)',
}


class OverpassSource(BaseSource):
    key = "overpass"
    name = "OpenStreetMap / Overpass"
    kind = "geo"
    required = True
    docs_url = "https://wiki.openstreetmap.org/wiki/Overpass_API"
    licence = "ODbL. Uso libre con atribucion a los colaboradores de OpenStreetMap."

    def fetch_pois(
        self, lat: float, lon: float, radius_km: float = 30.0, kinds: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Recupera POIs de los tipos indicados alrededor de un punto."""
        kinds = kinds or list(POI_QUERIES)
        radius_m = int(radius_km * 1000)
        blocks = []
        for kind in kinds:
            template = POI_QUERIES.get(kind)
            if template:
                blocks.append(template.replace("{bbox}", f"(around:{radius_m},{lat},{lon})"))
        if not blocks:
            return []

        query = f"[out:json][timeout:60];({''.join(blocks)});out center tags;"
        response = self.request("POST", self.settings.overpass_url, data={"data": query})
        if response.status_code != 200:
            raise SourceError(f"Overpass HTTP {response.status_code}")

        elements = response.json().get("elements", [])
        return [p for p in (self._normalize(e) for e in elements) if p]

    @staticmethod
    def _normalize(element: dict[str, Any]) -> dict[str, Any] | None:
        tags = element.get("tags", {})
        # Los 'way' y 'relation' traen su centroide en 'center'.
        center = element.get("center") or {}
        lat = element.get("lat", center.get("lat"))
        lon = element.get("lon", center.get("lon"))
        if lat is None or lon is None:
            return None

        if tags.get("natural") == "beach":
            kind = "beach"
        elif tags.get("natural") in ("peak", "volcano"):
            kind = "mountain"
        elif tags.get("landuse") == "winter_sports":
            kind = "ski"
        elif tags.get("leisure") == "marina":
            kind = "marina"
        elif tags.get("historic"):
            kind = "heritage"
        elif tags.get("tourism"):
            kind = "tourism"
        else:
            return None

        return {
            "source": "osm",
            "external_id": f"{element.get('type')}/{element.get('id')}",
            "name": tags.get("name", "") or kind,
            "kind": kind,
            "lat": float(lat),
            "lon": float(lon),
            "importance": 2.0 if tags.get("name") else 1.0,
        }

    def check(self) -> SourceStatus:
        return self._timed_probe(
            self.settings.overpass_url,
            method="POST",
            data={"data": "[out:json][timeout:10];out count;"},
        )


class NominatimSource(BaseSource):
    key = "nominatim"
    name = "Nominatim (geocodificacion OSM)"
    kind = "geo"
    required = False
    docs_url = "https://nominatim.org/release-docs/latest/api/Search/"
    licence = "ODbL. Maximo 1 peticion/segundo segun su politica de uso."

    def geocode(self, query: str) -> dict[str, Any] | None:
        response = self.request(
            "GET",
            f"{self.settings.nominatim_url}/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "es"},
        )
        if response.status_code != 200:
            raise SourceError(f"Nominatim HTTP {response.status_code}")
        results = response.json()
        if not results:
            return None
        top = results[0]
        return {
            "lat": float(top["lat"]),
            "lon": float(top["lon"]),
            "display_name": top.get("display_name", ""),
        }

    def reverse(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Municipio y provincia de un punto. Gratis y sin cuota.

        Lo usan los proveedores que buscan por nombre de zona en vez de por
        coordenadas, para no pedirle al usuario algo que ya se deduce del punto.
        """
        response = self.request(
            "GET",
            f"{self.settings.nominatim_url}/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
                    "addressdetails": 1},
        )
        if response.status_code != 200:
            raise SourceError(f"Nominatim reverse HTTP {response.status_code}")
        payload = response.json()
        address = payload.get("address", {})
        municipality = (
            address.get("city") or address.get("town") or address.get("village")
            or address.get("municipality") or address.get("county")
        )
        if not municipality:
            return None
        return {
            "municipality": municipality,
            "province": address.get("province") or address.get("state", ""),
            "display_name": payload.get("display_name", ""),
        }

    def check(self) -> SourceStatus:
        return self._timed_probe(
            f"{self.settings.nominatim_url}/search",
            params={"q": "Malaga", "format": "jsonv2", "limit": 1},
        )
