"""Tendencia del valor del suelo (funcionalidad 1).

Un unico numero no basta: un municipio puede tener un CAGR alto por un pico
puntual. Por eso se combinan tres medidas: CAGR (cuanto sube), R^2 de la
regresion (si sube de forma sostenida o a saltos) y la pendiente reciente
(si sigue subiendo ahora o ya giro).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class TrendResult:
    cagr: float | None            # crecimiento anual compuesto, en tanto por uno
    slope_eur_m2_year: float | None
    r_squared: float | None
    recent_cagr: float | None     # ultimos 3 periodos
    years_covered: int
    first_value: float | None
    last_value: float | None
    is_rising: bool
    confidence: str               # alta | media | baja | insuficiente

    def as_dict(self) -> dict:
        return {
            "cagr": self.cagr,
            "cagr_pct": round(self.cagr * 100, 2) if self.cagr is not None else None,
            "slope_eur_m2_year": self.slope_eur_m2_year,
            "r_squared": self.r_squared,
            "recent_cagr_pct": (
                round(self.recent_cagr * 100, 2) if self.recent_cagr is not None else None
            ),
            "years_covered": self.years_covered,
            "first_value": self.first_value,
            "last_value": self.last_value,
            "is_rising": self.is_rising,
            "confidence": self.confidence,
        }


def linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Minimos cuadrados. Devuelve (pendiente, ordenada, R^2)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx < 1e-12:
        return 0.0, mean_y, 0.0

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return slope, intercept, max(0.0, min(1.0, r_squared))


def compute_cagr(first: float, last: float, years: float) -> float | None:
    """Crecimiento anual compuesto. Sin sentido si el punto inicial no es positivo."""
    if first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def analyze_series(points: list[tuple[int, float]]) -> TrendResult:
    """Analiza una serie de (anio, eur/m2) ordenada o no.

    Si hay varios valores para el mismo anio se promedian, de forma que una
    serie trimestral y una anual se traten igual.
    """
    if not points:
        return TrendResult(None, None, None, None, 0, None, None, False, "insuficiente")

    by_year: dict[int, list[float]] = {}
    for year, value in points:
        if value and value > 0:
            by_year.setdefault(int(year), []).append(float(value))

    if len(by_year) < 2:
        only = next(iter(by_year.values()), [None])[0] if by_year else None
        return TrendResult(None, None, None, None, len(by_year), only, only, False, "insuficiente")

    years = sorted(by_year)
    values = [sum(by_year[y]) / len(by_year[y]) for y in years]

    slope, _, r_squared = linear_regression([float(y) for y in years], values)
    span = years[-1] - years[0]
    cagr = compute_cagr(values[0], values[-1], float(span))

    recent_cagr = None
    if len(years) >= 3:
        recent_span = years[-1] - years[-3]
        recent_cagr = compute_cagr(values[-3], values[-1], float(recent_span))

    is_rising = bool(cagr is not None and cagr > 0 and slope > 0)

    # La fiabilidad depende de cuantas observaciones hay, no del intervalo:
    # cinco anos consecutivos son cinco puntos, aunque el span sea de 4.
    observations = len(years)
    if observations >= 5 and r_squared >= 0.7:
        confidence = "alta"
    elif observations >= 3 and r_squared >= 0.4:
        confidence = "media"
    else:
        confidence = "baja"

    return TrendResult(
        cagr=cagr,
        slope_eur_m2_year=round(slope, 3),
        r_squared=round(r_squared, 3),
        recent_cagr=recent_cagr,
        years_covered=span,
        first_value=round(values[0], 2),
        last_value=round(values[-1], 2),
        is_rising=is_rising,
        confidence=confidence,
    )


def trend_score(result: TrendResult) -> float:
    """Puntua la tendencia de 0 a 100.

    Se premia el crecimiento sostenido, no el explosivo: por encima de un 12%
    anual el suelo suele estar ya recalentado, asi que la curva satura y
    despues penaliza ligeramente.
    """
    if result.cagr is None or not result.is_rising:
        return 0.0

    cagr_pct = result.cagr * 100
    if cagr_pct <= 0:
        base = 0.0
    elif cagr_pct <= 12:
        base = 100.0 * (1 - math.exp(-cagr_pct / 4.0))
    else:
        base = 100.0 * (1 - math.exp(-3.0)) - min((cagr_pct - 12) * 1.5, 25.0)

    # La consistencia pesa: una subida limpia vale mas que una erratica.
    quality = 0.6 + 0.4 * (result.r_squared or 0.0)

    # Si la tendencia reciente giro a la baja, se corrige con fuerza.
    if result.recent_cagr is not None and result.recent_cagr < 0:
        quality *= 0.5

    return round(max(0.0, min(100.0, base * quality)), 2)
