"""Orquestacion: une fuentes, analisis y simulacion en operaciones completas."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.analysis.geo import haversine_km
from app.analysis.market import Comparable, compare_to_market
from app.analysis.poi import PoiRef, analyze_location
from app.analysis.scoring import ScoreBreakdown, score_opportunity
from app.analysis.trend import TrendResult, analyze_series
from app.models import LandPricePoint, Listing, Municipality, OpportunityScore, Poi, RentalStat
from app.simulation.business_plan import RentalAssumptions, build_business_plan
from app.simulation.catalog import get_model
from app.simulation.costs import CostAssumptions, compute_investment
from app.simulation.render2d import render_site_plan_svg
from app.simulation.render_model import render_model_card_svg
from app.simulation.siteplan import SitePlanOptions, build_site_plan, synthetic_parcel
from app.sources.base import SourceError
from app.sources.catastro import CatastroSource

# Ocupacion y tarifa de respaldo cuando no hay dato real para la zona.
# Son deliberadamente conservadoras: es peor sobreestimar un negocio que
# quedarse corto.
FALLBACK_ADR_EUR = 95.0
FALLBACK_OCCUPANCY = 0.42


def analyze_listing(
    db: Session, listing: Listing, *, radius_km: float = 15.0
) -> tuple[ScoreBreakdown, dict[str, Any]]:
    """Analiza un anuncio contra su mercado, la tendencia local y su entorno."""
    universe = _load_comparables(db, listing, radius_km)
    target = Comparable(listing.price_eur_m2, listing.lat, listing.lon, listing.area_m2)

    municipality = listing.municipality
    fallback_median = municipality.land_price_eur_m2 if municipality else None
    comparison = compare_to_market(
        target, universe, radius_km=radius_km, fallback_median_eur_m2=fallback_median
    )

    trend = _load_trend(db, municipality)
    pois = _load_pois(db, listing.lat, listing.lon, radius_km=35.0)
    location = analyze_location(listing.lat, listing.lon, pois)

    breakdown = score_opportunity(comparison, trend, location, listing.area_m2)

    # Si las coordenadas no son del anuncio sino del municipio, las distancias
    # a playa o montaña son orientativas y la ficha no debe presentarlas como
    # exactas.
    precision = (listing.raw or {}).get("coords_precision", "exact")
    if precision != "exact":
        origen = {
            "municipality": "el centro del municipio",
            "search_center": "el centro de la búsqueda",
        }.get(precision, "una posición aproximada")
        breakdown.warnings.append(
            f"El anuncio no traía coordenadas propias: se ha situado en {origen}. "
            "Las distancias a playa, montaña y puntos de interés son aproximadas."
        )

    detail = {
        "market": comparison.as_dict(),
        "trend": trend.as_dict(),
        "location": location.as_dict(),
        "score": breakdown.as_dict(),
        "coords_precision": precision,
    }

    _persist_score(db, listing, breakdown, comparison, trend, location, detail)
    return breakdown, detail


def _load_comparables(db: Session, listing: Listing, radius_km: float) -> list[Comparable]:
    """Trae anuncios cercanos usando primero un filtro de caja para no leer todo."""
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / max(111.0 * abs(math.cos(math.radians(listing.lat))), 1e-6)
    statement = select(Listing).where(
        Listing.id != listing.id,
        Listing.active.is_(True),
        Listing.price_eur_m2 > 0,
        Listing.lat.between(listing.lat - delta_lat, listing.lat + delta_lat),
        Listing.lon.between(listing.lon - delta_lon, listing.lon + delta_lon),
    )
    return [
        Comparable(row.price_eur_m2, row.lat, row.lon, row.area_m2)
        for row in db.execute(statement).scalars()
    ]


def _load_trend(db: Session, municipality: Municipality | None) -> TrendResult:
    if municipality is None:
        return analyze_series([])
    points = db.execute(
        select(LandPricePoint.year, LandPricePoint.eur_m2).where(
            LandPricePoint.municipality_id == municipality.id
        )
    ).all()
    return analyze_series([(year, value) for year, value in points])


def _load_pois(db: Session, lat: float, lon: float, radius_km: float) -> list[PoiRef]:
    delta = radius_km / 111.0
    statement = select(Poi).where(
        Poi.lat.between(lat - delta, lat + delta),
        Poi.lon.between(lon - delta * 1.4, lon + delta * 1.4),
    )
    return [
        PoiRef(p.name, p.kind, p.lat, p.lon, p.importance) for p in db.execute(statement).scalars()
    ]


def _persist_score(
    db: Session,
    listing: Listing,
    breakdown: ScoreBreakdown,
    comparison,
    trend: TrendResult,
    location,
    detail: dict[str, Any],
) -> None:
    score = listing.score or OpportunityScore(listing_id=listing.id)
    score.total_score = breakdown.total
    score.undervaluation_score = breakdown.undervaluation
    score.trend_score = breakdown.trend
    score.location_score = breakdown.location
    score.size_score = breakdown.size
    score.market_median_eur_m2 = comparison.median_eur_m2
    score.discount_vs_market = comparison.discount
    score.market_sample_size = comparison.sample_size
    score.price_percentile = comparison.percentile
    score.cagr_5y = trend.cagr
    score.trend_r2 = trend.r_squared
    score.dist_beach_km = location.dist_beach_km
    score.dist_mountain_km = location.dist_mountain_km
    score.dist_poi_km = location.nearest_poi_km
    score.nearest_poi = location.nearest_poi
    score.explanation = detail
    score.computed_at = datetime.now(timezone.utc)
    db.add(score)
    db.commit()


def rental_market_for(
    db: Session, municipality: Municipality | None, lat: float, lon: float, bedrooms: int
) -> dict[str, Any]:
    """Busca tarifa y ocupacion reales para la zona.

    Primero el propio municipio; si no hay dato, el municipio con estadistica
    mas cercano dentro de 40 km, que para alquiler turistico es un comparable
    razonable. Si tampoco, valores de respaldo marcados como tales.
    """
    if municipality is not None:
        statement = (
            select(RentalStat)
            .where(RentalStat.municipality_id == municipality.id)
            .order_by(RentalStat.sample_size.desc())
        )
        for stat in db.execute(statement).scalars():
            if stat.bedrooms == bedrooms or stat.bedrooms == 0:
                return _rental_payload(stat, municipality.name, 0.0, "municipio")
        first = db.execute(statement).scalars().first()
        if first:
            return _rental_payload(first, municipality.name, 0.0, "municipio (otro tamano)")

    best: tuple[float, RentalStat, Municipality] | None = None
    for stat, muni in db.execute(
        select(RentalStat, Municipality).join(Municipality, RentalStat.municipality_id == Municipality.id)
    ).all():
        distance = haversine_km(lat, lon, muni.lat, muni.lon)
        if distance <= 40.0 and (best is None or distance < best[0]):
            best = (distance, stat, muni)

    if best:
        distance, stat, muni = best
        return _rental_payload(stat, muni.name, round(distance, 1), "municipio cercano")

    return {
        "adr_eur": FALLBACK_ADR_EUR,
        "occupancy_rate": FALLBACK_OCCUPANCY,
        "sample_size": 0,
        "source": "estimacion",
        "reference": "Sin datos reales para la zona",
        "distance_km": None,
        "basis": "valores de respaldo conservadores",
        "is_real_data": False,
    }


def _rental_payload(stat: RentalStat, reference: str, distance: float, basis: str) -> dict[str, Any]:
    return {
        "adr_eur": stat.adr_eur,
        "adr_p25_eur": stat.adr_p25_eur,
        "adr_p75_eur": stat.adr_p75_eur,
        "occupancy_rate": stat.occupancy_rate,
        "sample_size": stat.sample_size,
        "source": stat.source,
        "reference": reference,
        "distance_km": distance,
        "basis": basis,
        "is_real_data": True,
    }


def run_full_simulation(
    db: Session,
    *,
    listing: Listing | None,
    land_price_eur: float,
    parcel_area_m2: float,
    lat: float,
    lon: float,
    model_id: str,
    cost_assumptions: CostAssumptions,
    rental_overrides: dict[str, Any] | None = None,
    site_options: SitePlanOptions | None = None,
    parcel_geojson: dict[str, Any] | None = None,
    use_cadastre: bool = True,
    use_cadastral_area: bool = True,
) -> dict[str, Any]:
    """Ejecuta la simulacion completa: costes, implantacion y plan de negocio."""
    model = get_model(model_id)

    # --- implantacion sobre la parcela real -------------------------------
    ring = None
    approximate_parcel = False
    cadastre: dict[str, Any] = {"attempted": False, "found": False}
    geojson = parcel_geojson or (listing.parcel_geojson if listing else None)
    if geojson:
        ring = _extract_ring(geojson)

    # Sin geometría guardada se pide al Catastro la parcela que contiene el
    # punto. Es lo que convierte la simulación en algo real: la forma de la
    # parcela determina dónde cabe la casa y cuánto retranqueo queda.
    if not ring and use_cadastre:
        cadastre["attempted"] = True
        try:
            feature = CatastroSource().parcel_at(lat, lon)
        except SourceError as exc:
            cadastre["error"] = str(exc)[:200]
        except Exception as exc:  # una caída del Catastro no debe tumbar la simulación
            cadastre["error"] = f"{type(exc).__name__}: {exc}"[:200]
        else:
            if feature:
                ring = _extract_ring(feature)
                if ring:
                    properties = feature.get("properties", {})
                    cadastre.update(
                        found=True,
                        cadastral_ref=properties.get("cadastral_ref"),
                        official_area_m2=properties.get("official_area_m2"),
                    )
                    geojson = feature
                    official = properties.get("official_area_m2")
                    if use_cadastral_area and official and official > 0:
                        # La superficie oficial manda sobre la tecleada: es la
                        # que usará notaría, registro y el cálculo del ITP.
                        cadastre["area_replaced_from"] = parcel_area_m2
                        parcel_area_m2 = float(official)

    if not ring:
        ring = synthetic_parcel(parcel_area_m2, lat, lon)
        approximate_parcel = True

    site_plan = build_site_plan(
        parcel_ring_lonlat=ring, model=model, options=site_options or SitePlanOptions()
    )
    if approximate_parcel:
        motivo = (
            "El Catastro no devolvió parcela para esas coordenadas"
            if cadastre.get("attempted") and not cadastre.get("error")
            else cadastre.get("error", "no se consultó el Catastro")
        )
        site_plan.warnings.insert(
            0,
            f"Sin geometría catastral ({motivo}): se dibuja un rectángulo equivalente "
            "a la superficie indicada. La forma real puede cambiar la implantación.",
        )
    elif cadastre.get("found"):
        detalle = f"referencia {cadastre.get('cadastral_ref')}"
        if cadastre.get("area_replaced_from"):
            detalle += (
                f"; se usa su superficie oficial de {parcel_area_m2:.0f} m² en lugar "
                f"de los {cadastre['area_replaced_from']:.0f} m² indicados"
            )
        site_plan.warnings.insert(0, f"Parcela real del Catastro ({detalle}).")
    site_plan_dict = site_plan.as_dict()
    site_plan_dict["approximate"] = approximate_parcel
    site_plan_dict["cadastre"] = cadastre
    site_plan_dict["parcel_geojson"] = geojson
    site_plan_dict["svg"] = render_site_plan_svg(site_plan_dict)

    # La inversión se calcula después de resolver la parcela: si el Catastro
    # devuelve una superficie oficial distinta, es esa la que debe usarse.
    investment = compute_investment(
        land_price_eur=land_price_eur,
        parcel_area_m2=parcel_area_m2,
        model=model,
        assumptions=cost_assumptions,
    )

    # --- plan de negocio con datos reales de la zona ------------------------
    municipality = listing.municipality if listing else None
    market = rental_market_for(db, municipality, lat, lon, model.bedrooms)
    overrides = rental_overrides or {}

    rental = RentalAssumptions(
        adr_eur=float(overrides.get("adr_eur") or market["adr_eur"]),
        occupancy_rate=float(overrides.get("occupancy_rate") or market["occupancy_rate"]),
        zone_type=overrides.get("zone_type", _infer_zone_type(listing, site_plan_dict)),
    )
    for field_name in (
        "quality_premium", "management_fee", "platform_commission", "cleaning_cost_per_stay",
        "avg_stay_nights", "utilities_eur_month", "insurance_eur_year", "ibi_eur_year",
        "maintenance_rate", "tax_rate", "discount_rate", "loan_amount_eur", "loan_rate",
        "loan_years", "horizon_years", "annual_adr_growth", "ramp_up_year_1",
    ):
        if field_name in overrides and overrides[field_name] is not None:
            setattr(rental, field_name, type(getattr(rental, field_name))(overrides[field_name]))

    model_image_url = _resolve_model_image(model.id)

    plan = build_business_plan(
        investment_eur=investment.total_eur,
        area_m2=model.area_m2,
        bedrooms=model.bedrooms,
        assumptions=rental,
    )
    if not market["is_real_data"]:
        plan.warnings.insert(
            0,
            "No hay datos reales de alquiler turistico para esta zona: la tarifa y la "
            "ocupacion son estimaciones conservadoras. Importa el volcado de "
            "InsideAirbnb de la ciudad mas proxima para afinarlo.",
        )
    else:
        plan.warnings.insert(
            0,
            f"Tarifa y ocupacion tomadas de {market['source']} en {market['reference']} "
            f"({market['sample_size']} anuncios reales).",
        )

    return {
        "model": {
            **model.as_dict(),
            "preview_svg": render_model_card_svg(model, 460, 265),
            "image_url": model_image_url,
        },
        "investment": investment.as_dict(),
        "business_plan": plan.as_dict(),
        "site_plan": site_plan_dict,
        "market_reference": market,
        "summary": _summarize(investment, plan, model, parcel_area_m2),
    }


def _summarize(investment, plan, model, parcel_area_m2: float) -> dict[str, Any]:
    returns = plan.returns
    return {
        "total_investment_eur": round(investment.total_eur, 2),
        "investment_per_m2_built": round(investment.total_eur / model.area_m2, 2)
        if model.area_m2 else 0.0,
        "annual_revenue_eur": plan.revenue.get("gross_revenue_eur", 0.0),
        "annual_ebitda_eur": plan.profit_and_loss.get("ebitda_eur", 0.0),
        "net_yield_pct": round(returns.get("net_yield", 0.0) * 100, 2),
        "payback_years": returns.get("payback_years"),
        "irr_pct": round(returns["irr"] * 100, 2) if returns.get("irr") is not None else None,
        "npv_eur": returns.get("npv_eur"),
        "parcel_area_m2": parcel_area_m2,
    }


def _infer_zone_type(listing: Listing | None, site_plan: dict[str, Any]) -> str:
    """Elige el patron de estacionalidad a partir del entorno del anuncio."""
    if listing is None or listing.score is None:
        return "costa"
    score = listing.score
    if score.dist_beach_km is not None and score.dist_beach_km <= 8:
        return "costa"
    if score.dist_mountain_km is not None and score.dist_mountain_km <= 10:
        return "montana"
    return "rural"


def _extract_ring(geojson: dict[str, Any]) -> list[list[float]] | None:
    """Saca el anillo exterior de un Feature, Polygon o lista de coordenadas."""
    if not geojson:
        return None
    geometry = geojson.get("geometry", geojson)
    if geometry.get("type") == "Polygon":
        coordinates = geometry.get("coordinates") or []
        if coordinates and len(coordinates[0]) >= 4:
            return [list(c[:2]) for c in coordinates[0]]
    if geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
        if polygons and polygons[0] and len(polygons[0][0]) >= 4:
            return [list(c[:2]) for c in polygons[0][0]]
    return None
