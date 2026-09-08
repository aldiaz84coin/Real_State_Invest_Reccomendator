"""Geometria plana y geodesica sin dependencias binarias.

Se evita shapely/pyproj a proposito: son ruedas pesadas de compilar en la
imagen de Fly y aqui solo hacen falta poligonos simples. Todo el trabajo
metrico se hace en una proyeccion local equirectangular centrada en la
parcela, donde a escala de unos cientos de metros el error es despreciable.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_000.0

Point = tuple[float, float]
Ring = list[Point]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia sobre la superficie terrestre en kilometros."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)) / 1000.0


class LocalProjection:
    """Convierte entre lon/lat y metros locales alrededor de un origen."""

    def __init__(self, origin_lat: float, origin_lon: float) -> None:
        self.origin_lat = origin_lat
        self.origin_lon = origin_lon
        self._m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180.0
        self._m_per_deg_lon = self._m_per_deg_lat * math.cos(math.radians(origin_lat))

    def to_meters(self, lon: float, lat: float) -> Point:
        return (
            (lon - self.origin_lon) * self._m_per_deg_lon,
            (lat - self.origin_lat) * self._m_per_deg_lat,
        )

    def to_lonlat(self, x: float, y: float) -> Point:
        return (
            self.origin_lon + x / self._m_per_deg_lon,
            self.origin_lat + y / self._m_per_deg_lat,
        )

    def ring_to_meters(self, ring: Iterable[Sequence[float]]) -> Ring:
        return [self.to_meters(float(c[0]), float(c[1])) for c in ring]

    def ring_to_lonlat(self, ring: Iterable[Point]) -> list[list[float]]:
        return [list(self.to_lonlat(x, y)) for x, y in ring]


# -- operaciones sobre poligonos -----------------------------------------


def signed_area(ring: Ring) -> float:
    """Area con signo (formula del cordon de zapato). Positiva si es antihoraria."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_area(ring: Ring) -> float:
    return abs(signed_area(ring))


def centroid(ring: Ring) -> Point:
    """Centroide de area. Cae de vuelta a la media si el area es degenerada."""
    area = signed_area(ring)
    if abs(area) < 1e-9:
        n = max(len(ring), 1)
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)

    cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6 * area), cy / (6 * area))


def as_ccw(ring: Ring) -> Ring:
    """Normaliza a orientacion antihoraria y sin vertice de cierre duplicado."""
    clean = list(ring)
    if len(clean) > 1 and _close(clean[0], clean[-1]):
        clean = clean[:-1]
    if signed_area(clean) < 0:
        clean.reverse()
    return clean


def point_in_polygon(point: Point, ring: Ring) -> bool:
    """Test de cruce de rayo. Los puntos del borde cuentan como interiores."""
    x, y = point
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_cross:
                inside = not inside
    return inside


def distance_to_edges(point: Point, ring: Ring) -> float:
    """Distancia minima del punto al perimetro (sin signo)."""
    x, y = point
    best = float("inf")
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-12:
            best = min(best, math.hypot(x - ax, y - ay))
            continue
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length_sq))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def clip_halfplane(ring: Ring, point: Point, normal: Point) -> Ring:
    """Recorta el poligono al semiplano {p : (p - point) . normal >= 0}.

    Es el paso de Sutherland-Hodgman, base del retranqueo.
    """
    if not ring:
        return []

    px, py = point
    nx, ny = normal

    def side(p: Point) -> float:
        return (p[0] - px) * nx + (p[1] - py) * ny

    output: Ring = []
    n = len(ring)
    for i in range(n):
        current = ring[i]
        nxt = ring[(i + 1) % n]
        d_cur, d_next = side(current), side(nxt)

        if d_cur >= 0:
            output.append(current)
        if (d_cur >= 0) != (d_next >= 0):
            t = d_cur / (d_cur - d_next)
            output.append(
                (current[0] + t * (nxt[0] - current[0]), current[1] + t * (nxt[1] - current[1]))
            )
    return output


def shrink_polygon(ring: Ring, distance: float) -> Ring:
    """Retranquea el poligono hacia dentro la distancia dada, en metros.

    Recorta contra el semiplano interior de cada arista. Para poligonos
    convexos el resultado es exacto; para concavos es conservador (algo mas
    pequeno de lo estrictamente necesario), que es justo el lado seguro al
    calcular donde se puede edificar.
    """
    if distance <= 0:
        return list(ring)

    result = as_ccw(ring)
    n = len(result)
    if n < 3:
        return []

    source = list(result)
    for i in range(n):
        ax, ay = source[i]
        bx, by = source[(i + 1) % n]
        edge_len = math.hypot(bx - ax, by - ay)
        if edge_len < 1e-9:
            continue
        # En orientacion antihoraria, la normal interior es (-dy, dx)/|e|.
        nx, ny = -(by - ay) / edge_len, (bx - ax) / edge_len
        offset_point = (ax + nx * distance, ay + ny * distance)
        result = clip_halfplane(result, offset_point, (nx, ny))
        if len(result) < 3:
            return []
    return result


def shrink_polygon_edges(ring: Ring, distances: Sequence[float]) -> Ring:
    """Retranquea cada lindero su propia distancia.

    El retranqueo frontal y el lateral no son iguales en ninguna ordenanza.
    Aplicar el mayor a todo el perimetro, que es lo que se hacia antes, dejaba
    parcelas pequenas sin superficie edificable por una prudencia que la
    normativa no exige.
    """
    result = as_ccw(ring)
    n = len(result)
    if n < 3 or len(distances) < n:
        return []

    source = list(result)
    for i in range(n):
        distance = distances[i]
        if distance <= 0:
            continue
        ax, ay = source[i]
        bx, by = source[(i + 1) % n]
        edge_len = math.hypot(bx - ax, by - ay)
        if edge_len < 1e-9:
            continue
        # En orientacion antihoraria, la normal interior es (-dy, dx)/|e|.
        nx, ny = -(by - ay) / edge_len, (bx - ax) / edge_len
        result = clip_halfplane(result, (ax + nx * distance, ay + ny * distance), (nx, ny))
        if len(result) < 3:
            return []
    return result


def bounding_box(ring: Ring) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def rectangle(cx: float, cy: float, width: float, depth: float, angle_deg: float) -> Ring:
    """Rectangulo centrado en (cx, cy), rotado `angle_deg` en sentido antihorario."""
    theta = math.radians(angle_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    half_w, half_d = width / 2.0, depth / 2.0
    corners = [(-half_w, -half_d), (half_w, -half_d), (half_w, half_d), (-half_w, half_d)]
    return [(cx + x * cos_t - y * sin_t, cy + x * sin_t + y * cos_t) for x, y in corners]


def polygon_contains_polygon(outer: Ring, inner: Ring) -> bool:
    """Aproximacion: todos los vertices del interior caen dentro del exterior.

    Basta porque el poligono interior que colocamos siempre es convexo (un
    rectangulo), y el exterior ya viene convexificado por `shrink_polygon`.
    """
    return all(point_in_polygon(p, outer) for p in inner)


def _close(a: Point, b: Point, tol: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol
