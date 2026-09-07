"""Comparacion contra el mercado local (funcionalidad 2).

"Muy por debajo de la media" se mide contra la *mediana* de €/m2 de terrenos
comparables en la misma zona, no contra la media: unas pocas fincas de lujo
disparan la media y harian pasar por ganga lo que es precio normal.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from app.analysis.geo import haversine_km

# Por debajo de esta muestra el estadistico no es fiable y se rebaja la nota.
MIN_RELIABLE_SAMPLE = 8


@dataclass
class MarketComparison:
    median_eur_m2: float | None
    mean_eur_m2: float | None
    p25_eur_m2: float | None
    p75_eur_m2: float | None
    sample_size: int
    discount: float | None        # 1 - precio/mediana; 0.4 = 40% mas barato
    percentile: float | None      # posicion del anuncio dentro de la muestra
    radius_km: float
    reliable: bool

    def as_dict(self) -> dict:
        return {
            "median_eur_m2": self.median_eur_m2,
            "mean_eur_m2": self.mean_eur_m2,
            "p25_eur_m2": self.p25_eur_m2,
            "p75_eur_m2": self.p75_eur_m2,
            "sample_size": self.sample_size,
            "discount_pct": round(self.discount * 100, 1) if self.discount is not None else None,
            "percentile": self.percentile,
            "radius_km": self.radius_km,
            "reliable": self.reliable,
        }


@dataclass
class Comparable:
    """Anuncio comparable reducido a lo que necesita el calculo."""

    price_eur_m2: float
    lat: float
    lon: float
    area_m2: float


def select_comparables(
    target: Comparable,
    universe: list[Comparable],
    *,
    radius_km: float = 15.0,
    area_ratio: float = 3.0,
) -> list[Comparable]:
    """Filtra el universo a lo que de verdad es comparable con la parcela.

    Dos criterios: proximidad geografica y tamano del mismo orden. Comparar
    una parcela de 800 m2 con una finca de 50.000 m2 no dice nada, porque el
    €/m2 cae de forma sistematica con la superficie.
    """
    low, high = target.area_m2 / area_ratio, target.area_m2 * area_ratio
    selected = []
    for candidate in universe:
        if candidate.price_eur_m2 <= 0:
            continue
        if target.area_m2 > 0 and not (low <= candidate.area_m2 <= high):
            continue
        if haversine_km(target.lat, target.lon, candidate.lat, candidate.lon) > radius_km:
            continue
        selected.append(candidate)
    return selected


def compare_to_market(
    target: Comparable,
    universe: list[Comparable],
    *,
    radius_km: float = 15.0,
    fallback_median_eur_m2: float | None = None,
) -> MarketComparison:
    """Situa el anuncio frente a sus comparables.

    Si no hay muestra suficiente de anuncios se recurre a la serie oficial de
    precio del municipio, que es menos precisa pero siempre esta disponible.
    """
    comparables = select_comparables(target, universe, radius_km=radius_km)
    prices = sorted(c.price_eur_m2 for c in comparables)

    if len(prices) < 3:
        if fallback_median_eur_m2 and fallback_median_eur_m2 > 0:
            discount = 1.0 - (target.price_eur_m2 / fallback_median_eur_m2)
            return MarketComparison(
                median_eur_m2=round(fallback_median_eur_m2, 2),
                mean_eur_m2=None,
                p25_eur_m2=None,
                p75_eur_m2=None,
                sample_size=len(prices),
                discount=round(discount, 4),
                percentile=None,
                radius_km=radius_km,
                reliable=False,
            )
        return MarketComparison(None, None, None, None, len(prices), None, None, radius_km, False)

    median = statistics.median(prices)
    quantiles = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [prices[0], median, prices[-1]]
    below = sum(1 for p in prices if p < target.price_eur_m2)

    return MarketComparison(
        median_eur_m2=round(median, 2),
        mean_eur_m2=round(statistics.fmean(prices), 2),
        p25_eur_m2=round(quantiles[0], 2),
        p75_eur_m2=round(quantiles[2], 2),
        sample_size=len(prices),
        discount=round(1.0 - (target.price_eur_m2 / median), 4) if median > 0 else None,
        percentile=round(100.0 * below / len(prices), 1),
        radius_km=radius_km,
        reliable=len(prices) >= MIN_RELIABLE_SAMPLE,
    )


def undervaluation_score(comparison: MarketComparison) -> float:
    """Puntua de 0 a 100 lo infravalorada que esta la parcela.

    El objetivo del usuario es "muy por debajo de la media", asi que la escala
    no es lineal: un 10% de descuento es ruido de negociacion y apenas puntua,
    mientras que a partir del 40% se acerca al maximo. Descuentos superiores al
    75% no suben la nota porque casi siempre esconden un problema (sin acceso,
    inundable, servidumbre) mas que una oportunidad.
    """
    if comparison.discount is None:
        return 0.0

    discount_pct = comparison.discount * 100
    if discount_pct <= 5:
        base = 0.0
    elif discount_pct >= 75:
        base = 85.0
    else:
        base = 100.0 * (1 - pow(2.718281828, -(discount_pct - 5) / 18.0))

    # Una muestra corta no invalida el dato, pero si obliga a ser prudente.
    if not comparison.reliable:
        base *= 0.7 if comparison.sample_size >= 3 else 0.55

    return round(max(0.0, min(100.0, base)), 2)
