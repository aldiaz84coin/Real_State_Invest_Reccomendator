"""Puntuacion compuesta de oportunidad de inversion.

Combina las cuatro senales que pide el encargo: precio muy por debajo de
mercado, tendencia alcista del suelo, cercania a playa/montana/turismo y
tamano de parcela adecuado.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.market import MarketComparison, undervaluation_score
from app.analysis.poi import LocationAnalysis, location_score, size_score
from app.analysis.trend import TrendResult, trend_score

# El descuento manda porque es la condicion explicita del encargo, pero sin la
# tendencia y el emplazamiento una ganga puede ser suelo que nadie quiere.
WEIGHTS = {
    "undervaluation": 0.38,
    "trend": 0.27,
    "location": 0.25,
    "size": 0.10,
}

# Umbral por debajo del cual no se considera "muy por debajo de mercado".
MIN_DISCOUNT_FOR_OPPORTUNITY = 0.20


@dataclass
class ScoreBreakdown:
    total: float
    undervaluation: float
    trend: float
    location: float
    size: float
    qualifies: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "components": {
                "undervaluation": self.undervaluation,
                "trend": self.trend,
                "location": self.location,
                "size": self.size,
            },
            "weights": WEIGHTS,
            "qualifies": self.qualifies,
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


def score_opportunity(
    comparison: MarketComparison,
    trend: TrendResult,
    location: LocationAnalysis,
    area_m2: float,
) -> ScoreBreakdown:
    """Puntua una parcela de 0 a 100 y explica por que."""
    under = undervaluation_score(comparison)
    trend_pts = trend_score(trend)
    location_pts = location_score(location)
    size_pts = size_score(area_m2)

    total = (
        under * WEIGHTS["undervaluation"]
        + trend_pts * WEIGHTS["trend"]
        + location_pts * WEIGHTS["location"]
        + size_pts * WEIGHTS["size"]
    )

    reasons: list[str] = []
    warnings: list[str] = []

    if comparison.discount is not None:
        pct = comparison.discount * 100
        if pct >= 20:
            reasons.append(
                f"Precio un {pct:.0f}% por debajo de la mediana de la zona "
                f"({comparison.median_eur_m2} €/m2 sobre {comparison.sample_size} comparables)."
            )
        elif pct > 0:
            warnings.append(f"Solo un {pct:.0f}% por debajo de mercado: descuento discreto.")
        else:
            warnings.append(f"Precio un {abs(pct):.0f}% por encima de la mediana de la zona.")

        if pct >= 60:
            warnings.append(
                "Descuento muy grande: conviene comprobar acceso rodado, servidumbres, "
                "riesgo de inundacion y si el suelo es realmente edificable."
            )

    if not comparison.reliable:
        warnings.append(
            f"Muestra corta ({comparison.sample_size} comparables): el descuento es orientativo."
        )

    if trend.is_rising and trend.cagr:
        reasons.append(
            f"El suelo sube un {trend.cagr * 100:.1f}% anual desde hace "
            f"{trend.years_covered} anos (fiabilidad {trend.confidence})."
        )
    elif trend.cagr is not None and trend.cagr <= 0:
        warnings.append("La serie de precios de la zona no muestra tendencia alcista.")
    else:
        warnings.append("Sin serie historica suficiente para confirmar la tendencia.")

    if trend.recent_cagr is not None and trend.recent_cagr < 0 and trend.is_rising:
        warnings.append("La subida a largo plazo se ha girado a la baja en los ultimos anos.")

    reasons.extend(location.highlights)

    if area_m2 < 300:
        warnings.append(
            f"Parcela de {area_m2:.0f} m2: por debajo de la superficie minima "
            "habitual para vivienda en muchos planeamientos."
        )

    qualifies = (
        comparison.discount is not None
        and comparison.discount >= MIN_DISCOUNT_FOR_OPPORTUNITY
        and trend.is_rising
        and location_pts >= 25
    )

    return ScoreBreakdown(
        total=round(total, 2),
        undervaluation=under,
        trend=trend_pts,
        location=location_pts,
        size=size_pts,
        qualifies=qualifies,
        reasons=reasons,
        warnings=warnings,
    )
