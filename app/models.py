"""Modelo de datos."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Municipality(Base):
    """Municipio espanol, clave de union entre anuncios y series de precio."""

    __tablename__ = "municipalities"

    id: Mapped[int] = mapped_column(primary_key=True)
    ine_code: Mapped[str] = mapped_column(String(5), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    province: Mapped[str] = mapped_column(String(80), index=True)
    ccaa: Mapped[str] = mapped_column(String(80), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Metricas derivadas, recalculadas por el motor de analisis.
    land_price_eur_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    land_price_cagr_5y: Mapped[float | None] = mapped_column(Float, nullable=True)
    land_price_r2: Mapped[float | None] = mapped_column(Float, nullable=True)
    tourism_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    dist_beach_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    dist_mountain_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    adr_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    occupancy_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Oferta de alquiler turistico segun la estadistica experimental del INE,
    # que la mide rastreando las plataformas. Es la demanda turistica real del
    # municipio, y llega a toda Espana, no solo a las ciudades grandes.
    tourist_dwellings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tourist_beds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tourist_data_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    metrics_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    price_points: Mapped[list["LandPricePoint"]] = relationship(
        back_populates="municipality", cascade="all, delete-orphan"
    )
    listings: Mapped[list["Listing"]] = relationship(back_populates="municipality")


class LandPricePoint(Base):
    """Un punto de la serie historica de precio de suelo/vivienda de un municipio."""

    __tablename__ = "land_price_points"
    __table_args__ = (
        UniqueConstraint("municipality_id", "year", "quarter", "source", name="uq_price_point"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipalities.id"), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    quarter: Mapped[int] = mapped_column(Integer, default=0)  # 0 = dato anual
    eur_m2: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40))

    municipality: Mapped[Municipality] = relationship(back_populates="price_points")


class Listing(Base):
    """Anuncio de terreno en venta procedente de una fuente oficial."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_listing_source_id"),
        Index("ix_listing_geo", "lat", "lon"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")

    price_eur: Mapped[float] = mapped_column(Float)
    area_m2: Mapped[float] = mapped_column(Float)
    price_eur_m2: Mapped[float] = mapped_column(Float, index=True)

    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    address: Mapped[str] = mapped_column(Text, default="")
    municipality_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipalities.id"), nullable=True, index=True
    )
    municipality_name: Mapped[str] = mapped_column(String(120), default="")
    province: Mapped[str] = mapped_column(String(80), default="")

    land_type: Mapped[str] = mapped_column(String(40), default="unknown")
    buildable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cadastral_ref: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    parcel_geojson: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    municipality: Mapped[Municipality | None] = relationship(back_populates="listings")
    score: Mapped["OpportunityScore | None"] = relationship(
        back_populates="listing", cascade="all, delete-orphan", uselist=False
    )


class OpportunityScore(Base):
    """Resultado del motor de analisis para un anuncio concreto."""

    __tablename__ = "opportunity_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id"), unique=True, index=True
    )

    total_score: Mapped[float] = mapped_column(Float, index=True)
    undervaluation_score: Mapped[float] = mapped_column(Float)
    trend_score: Mapped[float] = mapped_column(Float)
    location_score: Mapped[float] = mapped_column(Float)
    size_score: Mapped[float] = mapped_column(Float)

    market_median_eur_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_vs_market: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    price_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)

    cagr_5y: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_r2: Mapped[float | None] = mapped_column(Float, nullable=True)

    dist_beach_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    dist_mountain_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    dist_poi_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    nearest_poi: Mapped[str | None] = mapped_column(String(160), nullable=True)

    explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    listing: Mapped[Listing] = relationship(back_populates="score")


class Poi(Base):
    """Punto de interes (playa, montana, casco historico, atraccion turistica)."""

    __tablename__ = "pois"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_poi_source_id"),
        Index("ix_poi_geo", "lat", "lon"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), default="osm")
    external_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(40), index=True)  # beach|mountain|tourism|heritage
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    importance: Mapped[float] = mapped_column(Float, default=1.0)


class RentalStat(Base):
    """Estadistica real de alquiler turistico agregada por municipio."""

    __tablename__ = "rental_stats"
    __table_args__ = (
        UniqueConstraint("municipality_id", "bedrooms", "source", name="uq_rental_stat"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipalities.id"), index=True)
    bedrooms: Mapped[int] = mapped_column(Integer, default=2)
    source: Mapped[str] = mapped_column(String(40))

    adr_eur: Mapped[float] = mapped_column(Float)            # tarifa media diaria
    adr_p25_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    adr_p75_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    occupancy_rate: Mapped[float] = mapped_column(Float)      # 0-1
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_date: Mapped[str] = mapped_column(String(20), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Simulation(Base):
    """Simulacion guardada: inversion + plan de negocio + implantacion."""

    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    prefab_model_id: Mapped[str] = mapped_column(String(60))
    inputs: Mapped[dict] = mapped_column(JSON)
    investment: Mapped[dict] = mapped_column(JSON)
    business_plan: Mapped[dict] = mapped_column(JSON)
    site_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SourceCheck(Base):
    """Historico de comprobaciones de acceso a cada fuente externa."""

    __tablename__ = "source_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
