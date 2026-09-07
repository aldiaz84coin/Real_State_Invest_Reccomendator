"""Puntuacion de emplazamiento: playa, montana y atractivo turistico."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.geo import haversine_km


@dataclass
class PoiRef:
    name: str
    kind: str
    lat: float
    lon: float
    importance: float = 1.0


@dataclass
class LocationAnalysis:
    dist_beach_km: float | None = None
    dist_mountain_km: float | None = None
    dist_tourism_km: float | None = None
    dist_ski_km: float | None = None
    dist_marina_km: float | None = None
    nearest_poi: str | None = None
    nearest_poi_km: float | None = None
    poi_count_10km: int = 0
    highlights: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dist_beach_km": self.dist_beach_km,
            "dist_mountain_km": self.dist_mountain_km,
            "dist_tourism_km": self.dist_tourism_km,
            "dist_ski_km": self.dist_ski_km,
            "dist_marina_km": self.dist_marina_km,
            "nearest_poi": self.nearest_poi,
            "nearest_poi_km": self.nearest_poi_km,
            "poi_count_10km": self.poi_count_10km,
            "highlights": self.highlights,
        }


# Cuanto pesa cada tipo de entorno y a partir de que distancia deja de aportar.
KIND_WEIGHTS: dict[str, tuple[float, float]] = {
    # tipo:        (peso, distancia en km donde el aporte ya es nulo)
    "beach": (34.0, 25.0),
    "ski": (16.0, 30.0),
    "mountain": (16.0, 20.0),
    "tourism": (18.0, 20.0),
    "marina": (10.0, 20.0),
    "heritage": (6.0, 20.0),
}


def analyze_location(lat: float, lon: float, pois: list[PoiRef]) -> LocationAnalysis:
    """Calcula distancias a cada tipo de entorno relevante."""
    analysis = LocationAnalysis()
    if not pois:
        return analysis

    nearest_by_kind: dict[str, tuple[float, PoiRef]] = {}
    overall: tuple[float, PoiRef] | None = None
    within_10km = 0

    for poi in pois:
        distance = haversine_km(lat, lon, poi.lat, poi.lon)
        if distance <= 10.0:
            within_10km += 1
        current = nearest_by_kind.get(poi.kind)
        if current is None or distance < current[0]:
            nearest_by_kind[poi.kind] = (distance, poi)
        if overall is None or distance < overall[0]:
            overall = (distance, poi)

    analysis.poi_count_10km = within_10km
    for kind, attribute in (
        ("beach", "dist_beach_km"),
        ("mountain", "dist_mountain_km"),
        ("tourism", "dist_tourism_km"),
        ("ski", "dist_ski_km"),
        ("marina", "dist_marina_km"),
    ):
        if kind in nearest_by_kind:
            setattr(analysis, attribute, round(nearest_by_kind[kind][0], 2))

    if overall:
        analysis.nearest_poi = overall[1].name
        analysis.nearest_poi_km = round(overall[0], 2)

    analysis.highlights = _build_highlights(analysis)
    return analysis


def _build_highlights(analysis: LocationAnalysis) -> list[str]:
    """Frases cortas para la ficha; solo lo que de verdad es un argumento."""
    notes: list[str] = []
    if analysis.dist_beach_km is not None and analysis.dist_beach_km <= 5:
        notes.append(f"Playa a {analysis.dist_beach_km} km")
    if analysis.dist_ski_km is not None and analysis.dist_ski_km <= 15:
        notes.append(f"Estacion de esqui a {analysis.dist_ski_km} km")
    if analysis.dist_mountain_km is not None and analysis.dist_mountain_km <= 10:
        notes.append(f"Montana a {analysis.dist_mountain_km} km")
    if analysis.dist_marina_km is not None and analysis.dist_marina_km <= 10:
        notes.append(f"Puerto deportivo a {analysis.dist_marina_km} km")
    if analysis.poi_count_10km >= 15:
        notes.append(f"{analysis.poi_count_10km} puntos de interes en 10 km")
    return notes


def location_score(analysis: LocationAnalysis) -> float:
    """Puntua el emplazamiento de 0 a 100.

    Cada tipo de entorno aporta su peso completo si esta a mano y decae de
    forma lineal hasta su distancia de corte. Se suman porque playa y montana
    no compiten: una parcela que tenga las dos vale mas para alquiler turistico
    que una que solo tenga una.
    """
    total = 0.0
    distances = {
        "beach": analysis.dist_beach_km,
        "ski": analysis.dist_ski_km,
        "mountain": analysis.dist_mountain_km,
        "tourism": analysis.dist_tourism_km,
        "marina": analysis.dist_marina_km,
    }

    for kind, distance in distances.items():
        if distance is None:
            continue
        weight, cutoff = KIND_WEIGHTS[kind]
        if distance >= cutoff:
            continue
        # A menos de 2 km se considera "en el sitio" y aporta el peso entero.
        if distance <= 2.0:
            total += weight
        else:
            total += weight * (1.0 - (distance - 2.0) / (cutoff - 2.0))

    # Densidad de servicios y atracciones alrededor, hasta 6 puntos extra.
    total += min(analysis.poi_count_10km / 5.0, 6.0)
    return round(max(0.0, min(100.0, total)), 2)


def size_score(area_m2: float, *, min_m2: float = 300.0, ideal_m2: float = 1000.0,
               max_m2: float = 5000.0) -> float:
    """Puntua si la parcela tiene "una dimension acorde" al proyecto.

    Para una vivienda prefabricada de uso turistico el optimo esta alrededor de
    los 1.000 m2: bastante para casa, terraza, aparcamiento y privacidad, sin
    pagar de mas por suelo que no se aprovecha. Por debajo del minimo legal
    tipico de parcela edificable la nota se hunde; muy por encima decae suave,
    porque el exceso encarece la compra sin aportar al negocio.
    """
    if area_m2 <= 0:
        return 0.0
    if area_m2 < min_m2:
        return round(max(0.0, 40.0 * (area_m2 / min_m2)), 2)
    if area_m2 <= ideal_m2:
        return round(60.0 + 40.0 * (area_m2 - min_m2) / (ideal_m2 - min_m2), 2)
    if area_m2 <= max_m2:
        return round(100.0 - 35.0 * (area_m2 - ideal_m2) / (max_m2 - ideal_m2), 2)
    # Fincas grandes: siguen sirviendo, pero el suelo sobrante no rinde.
    return round(max(25.0, 65.0 - 10.0 * (area_m2 - max_m2) / max_m2), 2)
