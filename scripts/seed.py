#!/usr/bin/env python3
"""Carga inicial de datos.

Uso:
    python -m scripts.seed municipios          # municipios base (sin red)
    python -m scripts.seed pois --radius 35    # POIs de OSM para cada municipio
    python -m scripts.seed precios fichero.csv # serie de precios desde CSV
    python -m scripts.seed alquiler            # volcados de InsideAirbnb

Los municipios se cargan sin red. El resto necesita salida a internet, asi que
lo normal es ejecutarlo ya desplegado en Fly:
    fly ssh console -C "python -m scripts.seed pois"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import LandPricePoint, Municipality, Poi, RentalStat
from app.sources.base import SourceError
from app.sources.osm import OverpassSource
from app.sources.prices import MivauLandPriceSource
from app.sources.rental import InsideAirbnbSource

DATA_FILE = Path(__file__).resolve().parent.parent / "app" / "data" / "municipalities.json"


def seed_municipalities() -> int:
    """Inserta el listado base de municipios. Idempotente."""
    init_db()
    rows = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    created = 0
    with SessionLocal() as db:
        for row in rows:
            exists = db.execute(
                select(Municipality.id).where(Municipality.ine_code == row["ine_code"])
            ).scalar_one_or_none()
            if exists:
                continue
            db.add(Municipality(**row))
            created += 1
        db.commit()
    print(f"Municipios: {created} nuevos de {len(rows)}")
    return created


def seed_pois(radius_km: float = 35.0, limit: int | None = None) -> int:
    """Descarga puntos de interes de OSM alrededor de cada municipio."""
    init_db()
    source = OverpassSource()
    created = 0
    with SessionLocal() as db:
        municipalities = db.execute(select(Municipality)).scalars().all()
        if limit:
            municipalities = municipalities[:limit]

        for index, municipality in enumerate(municipalities, 1):
            try:
                pois = source.fetch_pois(municipality.lat, municipality.lon, radius_km)
            except SourceError as exc:
                print(f"  [{index}/{len(municipalities)}] {municipality.name}: {exc}")
                continue

            added = 0
            for item in pois:
                exists = db.execute(
                    select(Poi.id).where(
                        Poi.source == item["source"], Poi.external_id == item["external_id"]
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                db.add(Poi(**item))
                added += 1
            db.commit()
            created += added
            print(f"  [{index}/{len(municipalities)}] {municipality.name}: +{added} POIs")
    print(f"POIs: {created} nuevos")
    return created


def seed_prices(csv_path: str) -> int:
    """Importa una serie de precios de suelo descargada del INE o del MIVAU.

    El CSV debe tener columnas municipio, anio y eur_m2 (admite ';' y coma
    decimal, que es como publican las descargas oficiales).
    """
    init_db()
    content = Path(csv_path).read_text(encoding="utf-8", errors="replace")
    rows = MivauLandPriceSource.parse_csv(content)
    created = skipped = 0

    with SessionLocal() as db:
        for row in rows:
            municipality = db.execute(
                select(Municipality).where(Municipality.name.ilike(row["municipality"].strip()))
            ).scalars().first()
            if municipality is None:
                skipped += 1
                continue
            exists = db.execute(
                select(LandPricePoint.id).where(
                    LandPricePoint.municipality_id == municipality.id,
                    LandPricePoint.year == row["year"],
                    LandPricePoint.quarter == 0,
                    LandPricePoint.source == "mivau",
                )
            ).scalar_one_or_none()
            if exists:
                continue
            db.add(LandPricePoint(
                municipality_id=municipality.id, year=row["year"], quarter=0,
                eur_m2=row["eur_m2"], source="mivau",
            ))
            created += 1
        db.commit()

        # El precio mas reciente se cachea en el municipio: lo usa el motor de
        # comparacion como respaldo cuando no hay comparables suficientes.
        for municipality in db.execute(select(Municipality)).scalars():
            latest = db.execute(
                select(LandPricePoint)
                .where(LandPricePoint.municipality_id == municipality.id)
                .order_by(LandPricePoint.year.desc())
            ).scalars().first()
            if latest:
                municipality.land_price_eur_m2 = latest.eur_m2
        db.commit()

    print(f"Precios: {created} puntos nuevos, {skipped} filas sin municipio conocido")
    return created


def seed_rental() -> int:
    """Importa los volcados de InsideAirbnb que casen con municipios cargados."""
    init_db()
    source = InsideAirbnbSource()
    try:
        datasets = source.discover_spain_datasets()
    except SourceError as exc:
        print(f"No se pudo listar InsideAirbnb: {exc}")
        return 0
    print(f"InsideAirbnb publica {len(datasets)} volcados de Espana")

    created = 0
    with SessionLocal() as db:
        municipalities = db.execute(select(Municipality)).scalars().all()
        for dataset in datasets:
            city = dataset["city"].replace("-", " ").lower()
            match = next(
                (m for m in municipalities
                 if city in m.name.lower() or m.name.lower() in city), None
            )
            if match is None:
                continue
            try:
                rows = source.fetch_listings(dataset["url"])
            except SourceError as exc:
                print(f"  {dataset['city']}: {exc}")
                continue

            aggregate = source.aggregate(rows)
            if not aggregate["sample_size"]:
                continue

            existing = db.execute(
                select(RentalStat).where(
                    RentalStat.municipality_id == match.id,
                    RentalStat.bedrooms == 2,
                    RentalStat.source == "insideairbnb",
                )
            ).scalar_one_or_none()
            stat = existing or RentalStat(
                municipality_id=match.id, bedrooms=2, source="insideairbnb"
            )
            stat.adr_eur = aggregate["adr_eur"]
            stat.adr_p25_eur = aggregate.get("adr_p25_eur")
            stat.adr_p75_eur = aggregate.get("adr_p75_eur")
            stat.occupancy_rate = aggregate["occupancy_rate"]
            stat.sample_size = aggregate["sample_size"]
            stat.snapshot_date = dataset["date"]
            db.add(stat)
            match.adr_eur = aggregate["adr_eur"]
            match.occupancy_rate = aggregate["occupancy_rate"]
            db.commit()
            created += 1
            print(f"  {match.name}: ADR {aggregate['adr_eur']} €, "
                  f"ocupacion {aggregate['occupancy_rate']:.0%}, "
                  f"{aggregate['sample_size']} anuncios")

    print(f"Alquiler turistico: {created} municipios con datos reales")
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga de datos iniciales")
    parser.add_argument(
        "command", choices=["municipios", "pois", "precios", "alquiler", "todo"]
    )
    parser.add_argument("csv", nargs="?", help="Ruta del CSV para el comando 'precios'")
    parser.add_argument("--radius", type=float, default=35.0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if args.command in ("municipios", "todo"):
        seed_municipalities()
    if args.command in ("pois", "todo"):
        seed_pois(args.radius, args.limit)
    if args.command == "precios":
        if not args.csv:
            parser.error("El comando 'precios' necesita la ruta de un CSV")
        seed_prices(args.csv)
    if args.command in ("alquiler", "todo"):
        seed_rental()


if __name__ == "__main__":
    main()
