"""Esquemas de entrada y salida de la API."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field


def _empty_to_none(value: Any) -> Any:
    """Un campo de formulario en blanco significa «sin valor», no un error."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalFloat = Annotated[float | None, BeforeValidator(_empty_to_none)]
OptionalInt = Annotated[int | None, BeforeValidator(_empty_to_none)]


class SimulationRequest(BaseModel):
    """Peticion de simulacion completa."""

    listing_id: int | None = None
    land_price_eur: float | None = Field(default=None, ge=0)
    parcel_area_m2: float | None = Field(default=None, gt=0)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    model_id: str = "plegable-40-2dorm"
    # Trae el polígono real de la parcela desde el Catastro en vez de dibujar
    # un rectángulo equivalente. Cambia por completo la implantación.
    use_cadastre: bool = True
    use_cadastral_area: bool = True   # usar también su superficie oficial

    # Costes
    ccaa: str = "Andalucia"
    seller_is_business: bool = False
    contingency_rate: float = Field(default=0.10, ge=0, le=0.5)
    include_pool: bool = False
    pool_eur: float = Field(default=0.0, ge=0)
    off_grid: bool = False

    # Implantacion
    setback_front_m: float = Field(default=5.0, ge=0, le=50)
    setback_sides_m: float = Field(default=3.0, ge=0, le=50)
    max_occupancy_rate: float = Field(default=0.30, gt=0, le=1)
    include_terrace: bool = True
    include_parking: bool = True

    # Explotacion
    adr_eur: float | None = Field(default=None, gt=0)
    occupancy_rate: float | None = Field(default=None, gt=0, le=1)
    zone_type: str | None = None
    management_fee: float | None = Field(default=None, ge=0, le=0.6)
    quality_premium: float | None = Field(default=None, ge=-0.5, le=1.0)
    tax_rate: float | None = Field(default=None, ge=0, le=0.6)
    loan_amount_eur: float | None = Field(default=None, ge=0)
    loan_rate: float | None = Field(default=None, ge=0, le=0.3)
    loan_years: int | None = Field(default=None, ge=1, le=40)
    horizon_years: int | None = Field(default=None, ge=1, le=40)

    save_as: str | None = None


class OpportunityQuery(BaseModel):
    """Filtros del buscador de oportunidades."""

    q: str | None = None
    province: str | None = None
    min_area_m2: OptionalFloat = Field(default=300, ge=0)
    max_area_m2: OptionalFloat = Field(default=None, ge=0)
    max_price_eur: OptionalFloat = Field(default=None, ge=0)
    min_discount_pct: OptionalFloat = Field(default=20.0, ge=0, le=95)
    max_beach_km: OptionalFloat = Field(default=None, ge=0)
    max_mountain_km: OptionalFloat = Field(default=None, ge=0)
    require_rising_trend: bool = True
    limit: int = Field(default=50, ge=1, le=500)


class IngestListingsRequest(BaseModel):
    """Descarga de anuncios desde Idealista alrededor de un punto."""

    lat: float
    lon: float
    radius_km: float = Field(default=20.0, gt=0, le=50)
    min_size_m2: float | None = None
    max_size_m2: float | None = None
    max_price_eur: float | None = None
    max_pages: int = Field(default=2, ge=1, le=10)


class IngestPoisRequest(BaseModel):
    lat: float
    lon: float
    radius_km: float = Field(default=30.0, gt=0, le=80)
    kinds: list[str] | None = None


class IngestRentalRequest(BaseModel):
    """Carga de un volcado de InsideAirbnb para una ciudad."""

    municipality_ine_code: str
    dataset_url: str | None = None
    bedrooms: int = Field(default=2, ge=0, le=10)


class ApiResponse(BaseModel):
    ok: bool = True
    data: Any = None
    message: str = ""
