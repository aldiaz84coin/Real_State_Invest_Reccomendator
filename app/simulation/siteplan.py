"""Implantacion de la vivienda sobre la parcela real (funcionalidad 5).

Toma el poligono catastral de la parcela y las dimensiones del modelo elegido,
aplica los retranqueos, busca la mejor colocacion y devuelve una escena con
todos los elementos en metros locales y en lon/lat. Esa misma escena alimenta
los dos renderizadores: el plano 2D en SVG y la vista 3D con Three.js.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.analysis.geo import (
    LocalProjection,
    Ring,
    as_ccw,
    bounding_box,
    centroid,
    distance_to_edges,
    point_in_polygon,
    polygon_area,
    polygon_contains_polygon,
    rectangle,
    shrink_polygon,
)
from app.simulation.catalog import PrefabModel, get_model

PARKING_LENGTH_M = 5.0
PARKING_WIDTH_M = 2.5
TERRACE_DEPTH_M = 3.0
POOL_LENGTH_M = 8.0
POOL_WIDTH_M = 4.0


@dataclass
class SitePlanOptions:
    """Parametros urbanisticos y de programa.

    Los retranqueos por defecto (3 m a linderos, 5 m a frente) son los mas
    habituales en suelo urbano de baja densidad, pero los fija cada
    planeamiento municipal, asi que se pueden ajustar.
    """

    setback_front_m: float = 5.0
    setback_sides_m: float = 3.0
    max_occupancy_rate: float = 0.30    # ocupacion maxima de parcela
    max_buildability_m2_m2: float = 0.30
    include_terrace: bool = True
    include_parking: bool = True
    include_pool: bool = False
    orient_to_south: bool = True
    access_lat: float | None = None
    access_lon: float | None = None


@dataclass
class SitePlan:
    parcel: dict = field(default_factory=dict)
    buildable: dict = field(default_factory=dict)
    house: dict = field(default_factory=dict)
    terrace: dict | None = None
    parking: dict | None = None
    pool: dict | None = None
    driveway: list[list[float]] = field(default_factory=list)
    driveway_meters: list[list[float]] = field(default_factory=list)
    trees: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    compliance: dict = field(default_factory=dict)
    origin: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    feasible: bool = True

    def as_dict(self) -> dict:
        return {
            "parcel": self.parcel,
            "buildable": self.buildable,
            "house": self.house,
            "terrace": self.terrace,
            "parking": self.parking,
            "pool": self.pool,
            "driveway": self.driveway,
            "driveway_meters": self.driveway_meters,
            "trees": self.trees,
            "metrics": self.metrics,
            "compliance": self.compliance,
            "origin": self.origin,
            "warnings": self.warnings,
            "feasible": self.feasible,
        }


def synthetic_parcel(area_m2: float, lat: float, lon: float, ratio: float = 1.35) -> list[list[float]]:
    """Parcela rectangular aproximada cuando no hay geometria catastral.

    Permite que la simulacion funcione igualmente con un anuncio que solo
    declara superficie. Se marca como aproximada en los avisos.
    """
    area = max(area_m2, 1.0)
    width = math.sqrt(area / ratio)
    depth = width * ratio
    projection = LocalProjection(lat, lon)
    ring = rectangle(0.0, 0.0, width, depth, 0.0)
    return [list(projection.to_lonlat(x, y)) for x, y in ring] + [
        list(projection.to_lonlat(*ring[0]))
    ]


def build_site_plan(
    *,
    parcel_ring_lonlat: Sequence[Sequence[float]],
    model: PrefabModel | str,
    options: SitePlanOptions | None = None,
) -> SitePlan:
    """Calcula la implantacion optima de la vivienda en la parcela."""
    if isinstance(model, str):
        model = get_model(model)
    opts = options or SitePlanOptions()
    plan = SitePlan()
    warnings: list[str] = []

    ring_lonlat = [list(map(float, c[:2])) for c in parcel_ring_lonlat]
    if len(ring_lonlat) < 4:
        plan.feasible = False
        plan.warnings = ["La parcela no tiene un poligono valido."]
        return plan

    # Origen local en el centroide, para que los metros sean manejables.
    lons = [c[0] for c in ring_lonlat]
    lats = [c[1] for c in ring_lonlat]
    origin_lon = sum(lons) / len(lons)
    origin_lat = sum(lats) / len(lats)
    projection = LocalProjection(origin_lat, origin_lon)

    parcel: Ring = as_ccw(projection.ring_to_meters(ring_lonlat))
    parcel_area = polygon_area(parcel)
    perimeter = _perimeter(parcel)

    # --- superficie edificable tras retranqueos ---------------------------
    # Se aplica el retranqueo mas restrictivo a todo el perimetro: es el
    # criterio prudente cuando no se sabe cual es el lindero frontal.
    setback = max(opts.setback_front_m, opts.setback_sides_m)
    buildable = shrink_polygon(parcel, setback)
    buildable_area = polygon_area(buildable) if len(buildable) >= 3 else 0.0

    if buildable_area <= 0:
        plan.feasible = False
        plan.parcel = _polygon_payload(parcel, projection)
        plan.warnings = [
            f"Con retranqueos de {setback:.0f} m no queda superficie edificable en una "
            f"parcela de {parcel_area:.0f} m2."
        ]
        plan.metrics = {"parcel_area_m2": round(parcel_area, 1), "buildable_area_m2": 0.0}
        plan.origin = {"lat": origin_lat, "lon": origin_lon}
        return plan

    # --- punto de acceso ---------------------------------------------------
    if opts.access_lat is not None and opts.access_lon is not None:
        access = projection.to_meters(opts.access_lon, opts.access_lat)
        access = _closest_point_on_ring(access, parcel)
    else:
        access = _longest_edge_midpoint(parcel)

    # --- colocacion de la vivienda -----------------------------------------
    placement = _find_placement(buildable, model, access, opts)
    if placement is None:
        plan.feasible = False
        plan.parcel = _polygon_payload(parcel, projection)
        plan.buildable = _polygon_payload(buildable, projection)
        plan.warnings = [
            f"El modulo de {model.length_m:.1f} x {model.width_m:.1f} m no cabe dentro "
            f"del area edificable ({buildable_area:.0f} m2) con los retranqueos actuales. "
            "Prueba a reducir los retranqueos o a elegir un modelo mas pequeno."
        ]
        plan.metrics = {
            "parcel_area_m2": round(parcel_area, 1),
            "buildable_area_m2": round(buildable_area, 1),
        }
        plan.origin = {"lat": origin_lat, "lon": origin_lon}
        return plan

    cx, cy, angle, clearance = placement
    house_ring = rectangle(cx, cy, model.length_m, model.width_m, angle)
    footprint = model.length_m * model.width_m

    # --- terraza al sur ------------------------------------------------------
    terrace_ring: Ring | None = None
    if opts.include_terrace:
        terrace_ring = _attach_terrace(cx, cy, model, angle, TERRACE_DEPTH_M)
        if not polygon_contains_polygon(buildable, terrace_ring):
            # Si la terraza se sale del area edificable se recorta a la mitad
            # antes que renunciar a ella: es el elemento que mas valor aporta
            # a un alojamiento turistico.
            terrace_ring = _attach_terrace(cx, cy, model, angle, TERRACE_DEPTH_M / 2)
            if not polygon_contains_polygon(buildable, terrace_ring):
                terrace_ring = None
                warnings.append("No cabe terraza dentro del area edificable con estos retranqueos.")

    # --- aparcamiento y camino ------------------------------------------------
    parking_ring: Ring | None = None
    driveway: list[list[float]] = []
    driveway_meters: list[list[float]] = []
    if opts.include_parking:
        parking_ring = _place_parking(parcel, access, house_ring, terrace_ring)
        if parking_ring:
            route = [access, centroid(parking_ring), (cx, cy)]
            driveway = projection.ring_to_lonlat(route)
            driveway_meters = [[round(x, 3), round(y, 3)] for x, y in route]
        else:
            warnings.append("No se ha encontrado hueco para plaza de aparcamiento.")

    # --- piscina ---------------------------------------------------------------
    pool_ring: Ring | None = None
    if opts.include_pool:
        obstacles = [house_ring] + [r for r in (terrace_ring, parking_ring) if r]
        pool_ring = _place_pool(buildable, obstacles, cx, cy, angle)
        if not pool_ring:
            warnings.append("No cabe piscina sin invadir la casa, la terraza o los retranqueos.")

    # --- arbolado (solo decorativo en el render) ---------------------------------
    obstacles = [house_ring] + [r for r in (terrace_ring, parking_ring, pool_ring) if r]
    trees = _scatter_trees(parcel, obstacles, projection)

    # --- cumplimiento urbanistico ------------------------------------------------
    built_area = footprint + (polygon_area(terrace_ring) * 0.5 if terrace_ring else 0.0)
    occupancy_rate = footprint / parcel_area if parcel_area else 0.0
    buildability = model.area_m2 / parcel_area if parcel_area else 0.0

    compliance = {
        "occupancy_rate": round(occupancy_rate, 4),
        "max_occupancy_rate": opts.max_occupancy_rate,
        "occupancy_ok": occupancy_rate <= opts.max_occupancy_rate,
        "buildability_m2_m2": round(buildability, 4),
        "max_buildability_m2_m2": opts.max_buildability_m2_m2,
        "buildability_ok": buildability <= opts.max_buildability_m2_m2,
        "setback_applied_m": setback,
        "min_clearance_m": round(clearance, 2),
    }
    if not compliance["occupancy_ok"]:
        warnings.append(
            f"La huella ocupa el {occupancy_rate * 100:.1f}% de la parcela y el limite "
            f"supuesto es el {opts.max_occupancy_rate * 100:.0f}%. Confirma la ordenanza."
        )
    if not compliance["buildability_ok"]:
        warnings.append(
            f"Edificabilidad de {buildability:.2f} m2/m2 sobre un maximo supuesto de "
            f"{opts.max_buildability_m2_m2:.2f}. Confirma el planeamiento."
        )
    warnings.append(
        "Retranqueos, ocupacion y edificabilidad son valores por defecto: los fija la "
        "ordenanza de cada municipio y hay que verificarlos antes de comprar."
    )

    plan.parcel = _polygon_payload(parcel, projection, extra={"area_m2": round(parcel_area, 1)})
    plan.buildable = _polygon_payload(
        buildable, projection, extra={"area_m2": round(buildable_area, 1)}
    )
    plan.house = {
        **_polygon_payload(house_ring, projection),
        "height_m": model.height_m,
        "roof_height_m": round(model.height_m + 0.45, 2),
        "angle_deg": round(angle, 1),
        "area_m2": round(footprint, 1),
        "model_id": model.id,
        "model_name": model.name,
        "bedrooms": model.bedrooms,
        "door": projection.ring_to_lonlat([_south_edge_midpoint(house_ring)])[0],
    }
    if terrace_ring:
        plan.terrace = {
            **_polygon_payload(terrace_ring, projection),
            "area_m2": round(polygon_area(terrace_ring), 1),
            "height_m": 0.25,
        }
    if parking_ring:
        plan.parking = {
            **_polygon_payload(parking_ring, projection),
            "area_m2": round(polygon_area(parking_ring), 1),
            "height_m": 0.05,
        }
    if pool_ring:
        plan.pool = {
            **_polygon_payload(pool_ring, projection),
            "area_m2": round(polygon_area(pool_ring), 1),
            "depth_m": 1.4,
        }
    plan.driveway = driveway
    plan.driveway_meters = driveway_meters
    plan.trees = trees
    plan.metrics = {
        "parcel_area_m2": round(parcel_area, 1),
        "parcel_perimeter_m": round(perimeter, 1),
        "buildable_area_m2": round(buildable_area, 1),
        "house_footprint_m2": round(footprint, 1),
        "house_area_m2": model.area_m2,
        "terrace_area_m2": round(polygon_area(terrace_ring), 1) if terrace_ring else 0.0,
        "built_area_m2": round(built_area, 1),
        "free_area_m2": round(parcel_area - footprint, 1),
        "bbox_m": [round(v, 1) for v in bounding_box(parcel)],
    }
    plan.compliance = compliance
    plan.origin = {"lat": origin_lat, "lon": origin_lon}
    plan.warnings = warnings
    return plan


# -- colocacion --------------------------------------------------------------


def _find_placement(
    buildable: Ring, model: PrefabModel, access: tuple[float, float], opts: SitePlanOptions
) -> tuple[float, float, float, float] | None:
    """Busca centro y giro de la casa dentro del area edificable.

    Se prueban las orientaciones de los propios linderos (una casa paralela a
    la parcela es lo que se construye en la practica) sobre una rejilla de
    posiciones, y se elige la que maximiza holgura, orientacion sur y cercania
    al acceso.
    """
    angles = sorted({round(a) % 180 for a in _edge_angles(buildable)} | {0, 90})
    min_x, min_y, max_x, max_y = bounding_box(buildable)

    span = max(max_x - min_x, max_y - min_y)
    step = max(0.75, span / 28.0)

    best: tuple[float, float, float, float] | None = None
    best_score = float("-inf")

    for angle in angles:
        y = min_y
        while y <= max_y:
            x = min_x
            while x <= max_x:
                if point_in_polygon((x, y), buildable):
                    candidate = rectangle(x, y, model.length_m, model.width_m, float(angle))
                    if polygon_contains_polygon(buildable, candidate):
                        clearance = min(distance_to_edges(p, buildable) for p in candidate)
                        score = _placement_score(x, y, float(angle), clearance, access, opts)
                        if score > best_score:
                            best_score = score
                            best = (x, y, float(angle), clearance)
                x += step
            y += step

    return best


def _placement_score(
    x: float,
    y: float,
    angle: float,
    clearance: float,
    access: tuple[float, float],
    opts: SitePlanOptions,
) -> float:
    """Combina holgura, soleamiento y longitud del camino de acceso."""
    score = clearance * 3.0

    if opts.orient_to_south:
        # El lado largo mirando al sur (angulo cerca de 0 o 180) da mejor
        # soleamiento a la fachada principal y a la terraza.
        deviation = min(abs(angle % 180), 180 - abs(angle % 180))
        score += 6.0 * math.cos(math.radians(2 * deviation))
        # Colocar la casa hacia el norte de la parcela deja el jardin al sur.
        score += y * 0.10

    # Un camino largo es obra y coste, asi que penaliza en proporcion suave.
    score -= 0.12 * math.hypot(x - access[0], y - access[1])
    return score


def _attach_terrace(
    cx: float, cy: float, model: PrefabModel, angle: float, depth: float
) -> Ring:
    """Terraza pegada a la fachada mas soleada de la casa."""
    theta = math.radians(angle)
    # Vector perpendicular al lado largo; se elige el sentido que apunta al sur.
    nx, ny = -math.sin(theta), math.cos(theta)
    if ny > 0:
        nx, ny = -nx, -ny

    offset = (model.width_m + depth) / 2.0
    return rectangle(cx + nx * offset, cy + ny * offset, model.length_m, depth, angle)


def _place_parking(
    parcel: Ring,
    access: tuple[float, float],
    house: Ring,
    terrace: Ring | None,
) -> Ring | None:
    """Plaza de aparcamiento cerca del acceso, sin pisar casa ni terraza.

    Puede quedar fuera del area edificable (dentro del retranqueo), que es lo
    normal: aparcar en el retranqueo frontal esta permitido casi siempre por
    no ser edificacion.
    """
    obstacles = [house] + ([terrace] if terrace else [])
    house_center = centroid(house)
    direction = math.degrees(math.atan2(house_center[1] - access[1], house_center[0] - access[0]))

    for distance in (4.0, 6.0, 8.0, 10.0, 13.0, 16.0):
        for angle in (direction, direction + 90, direction + 45, direction - 45):
            theta = math.radians(direction)
            px = access[0] + math.cos(theta) * distance
            py = access[1] + math.sin(theta) * distance
            candidate = rectangle(px, py, PARKING_LENGTH_M, PARKING_WIDTH_M, angle)
            if not polygon_contains_polygon(parcel, candidate):
                continue
            if any(_overlaps(candidate, obstacle) for obstacle in obstacles):
                continue
            return candidate
    return None


def _place_pool(
    buildable: Ring, obstacles: list[Ring], cx: float, cy: float, angle: float
) -> Ring | None:
    """Piscina en el mejor hueco libre, preferiblemente al sur de la casa.

    Se recorre el area edificable en rejilla, igual que con la vivienda, en
    lugar de probar unas pocas posiciones fijas: en parcelas estrechas o
    irregulares esas posiciones fallaban aunque hubiera sitio de sobra.
    """
    min_x, min_y, max_x, max_y = bounding_box(buildable)
    step = max(0.6, max(max_x - min_x, max_y - min_y) / 30.0)

    best: Ring | None = None
    best_score = float("-inf")

    y = min_y
    while y <= max_y:
        x = min_x
        while x <= max_x:
            candidate = rectangle(x, y, POOL_LENGTH_M, POOL_WIDTH_M, angle)
            if polygon_contains_polygon(buildable, candidate) and not any(
                _overlaps(candidate, obstacle) for obstacle in obstacles
            ):
                # Al sur de la casa (y menor) y con separacion comoda de ella.
                separation = min(
                    min(distance_to_edges(p, obstacle) for p in candidate)
                    for obstacle in obstacles
                ) if obstacles else 10.0
                score = (cy - y) * 1.5 + min(separation, 6.0) * 2.0 - abs(x - cx) * 0.4
                if score > best_score:
                    best_score = score
                    best = candidate
            x += step
        y += step

    return best


def _scatter_trees(
    parcel: Ring, obstacles: list[Ring], projection: LocalProjection, target: int = 14
) -> list[dict]:
    """Reparte arbolado en el espacio libre. Es ambientacion del render 3D."""
    min_x, min_y, max_x, max_y = bounding_box(parcel)
    inner = shrink_polygon(parcel, 1.5)
    if len(inner) < 3:
        return []

    trees: list[dict] = []
    # Rejilla desplazada en lugar de aleatoriedad: el resultado es reproducible
    # entre ejecuciones, que importa cuando la escena va en un informe.
    steps = max(4, int(math.sqrt(target)) + 2)
    dx = (max_x - min_x) / steps
    dy = (max_y - min_y) / steps

    for i in range(steps + 1):
        for j in range(steps + 1):
            if len(trees) >= target:
                break
            x = min_x + dx * (i + (0.5 if j % 2 else 0.15))
            y = min_y + dy * (j + 0.35)
            if not point_in_polygon((x, y), inner):
                continue
            # Separacion minima de 2,5 m a cualquier elemento construido.
            if any(
                point_in_polygon((x, y), obstacle)
                or distance_to_edges((x, y), obstacle) < 2.5
                for obstacle in obstacles
            ):
                continue
            lon, lat = projection.to_lonlat(x, y)
            trees.append({
                "x": round(x, 2),
                "y": round(y, 2),
                "lon": lon,
                "lat": lat,
                "height_m": round(3.5 + ((i * 7 + j * 3) % 5) * 0.6, 1),
                "radius_m": round(1.4 + ((i * 3 + j * 5) % 4) * 0.25, 2),
            })
    return trees


# -- utilidades ----------------------------------------------------------------


def _polygon_payload(
    ring: Ring, projection: LocalProjection, extra: dict[str, Any] | None = None
) -> dict:
    """Entrega el poligono en metros locales y en lon/lat a la vez.

    Los metros los usan el SVG y Three.js; el lon/lat, el mapa Leaflet.
    """
    payload: dict[str, Any] = {
        "meters": [[round(x, 3), round(y, 3)] for x, y in ring],
        "lonlat": projection.ring_to_lonlat(ring),
    }
    if extra:
        payload.update(extra)
    return payload


def _perimeter(ring: Ring) -> float:
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def _edge_angles(ring: Ring) -> list[float]:
    angles = []
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if math.hypot(x2 - x1, y2 - y1) < 1.0:
            continue
        angles.append(math.degrees(math.atan2(y2 - y1, x2 - x1)))
    return angles or [0.0]


def _longest_edge_midpoint(ring: Ring) -> tuple[float, float]:
    best_length = -1.0
    best_point = ring[0]
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        length = math.hypot(x2 - x1, y2 - y1)
        if length > best_length:
            best_length = length
            best_point = ((x1 + x2) / 2, (y1 + y2) / 2)
    return best_point


def _closest_point_on_ring(point: tuple[float, float], ring: Ring) -> tuple[float, float]:
    x, y = point
    best = ring[0]
    best_distance = float("inf")
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-12:
            continue
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length_sq))
        px, py = ax + t * dx, ay + t * dy
        distance = math.hypot(x - px, y - py)
        if distance < best_distance:
            best_distance = distance
            best = (px, py)
    return best


def _south_edge_midpoint(ring: Ring) -> tuple[float, float]:
    """Punto medio del lado mas al sur: ahi se dibuja la puerta de entrada."""
    n = len(ring)
    best = ring[0]
    best_y = float("inf")
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        mid_y = (y1 + y2) / 2
        if mid_y < best_y:
            best_y = mid_y
            best = ((x1 + x2) / 2, mid_y)
    return best


def _overlaps(a: Ring, b: Ring) -> bool:
    """Solapamiento aproximado entre dos convexos, suficiente para separarlos."""
    return (
        any(point_in_polygon(p, b) for p in a)
        or any(point_in_polygon(p, a) for p in b)
        or point_in_polygon(centroid(a), b)
    )
