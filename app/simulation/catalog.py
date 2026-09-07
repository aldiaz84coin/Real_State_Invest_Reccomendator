"""Catalogo de viviendas prefabricadas seleccionables.

El modelo de referencia que pidio el usuario es una casa plegable
personalizable de contenedor vendida en Amazon.es; el catalogo lo recoge junto
a alternativas equivalentes y a modulares de gama superior, para poder comparar.

Los precios son valores por defecto editables desde la interfaz: son ordenes de
magnitud del mercado espanol, no ofertas en firme. Todo importe se puede
sobreescribir en la simulacion.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PrefabModel:
    id: str
    name: str
    family: str                  # plegable | expandible | modular
    area_m2: float
    length_m: float
    width_m: float
    height_m: float
    bedrooms: int
    bathrooms: int
    base_price_eur: float        # precio del modulo puesto en fabrica/puerto
    transport_eur: float         # transporte medio a peninsula
    assembly_eur: float          # montaje y despliegue en parcela
    needs_crane: bool
    crane_eur: float
    finish_level: str            # basico | medio | alto
    insulation: str
    warranty_years: int
    lead_time_weeks: int
    reference_url: str
    notes: str = ""

    @property
    def turnkey_module_eur(self) -> float:
        """Coste del modulo entregado y montado, sin obra de parcela."""
        crane = self.crane_eur if self.needs_crane else 0.0
        return self.base_price_eur + self.transport_eur + self.assembly_eur + crane

    @property
    def price_per_m2(self) -> float:
        return round(self.turnkey_module_eur / self.area_m2, 2) if self.area_m2 else 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["turnkey_module_eur"] = round(self.turnkey_module_eur, 2)
        data["price_per_m2"] = self.price_per_m2
        return data


# El primero es el modelo de referencia indicado por el usuario.
AMAZON_REFERENCE_URL = (
    "https://www.amazon.es/Plegable-Personalizable-prefabricada-dormitorios-Vivienda/dp/B0GHXJ46GM"
)

CATALOG: list[PrefabModel] = [
    PrefabModel(
        id="plegable-40-2dorm",
        name="Casa plegable personalizable 40' - 2 dormitorios (modelo de referencia)",
        family="plegable",
        area_m2=36.0,
        length_m=12.0,
        width_m=3.0,
        height_m=2.8,
        bedrooms=2,
        bathrooms=1,
        base_price_eur=19500.0,
        transport_eur=2200.0,
        assembly_eur=2500.0,
        needs_crane=True,
        crane_eur=900.0,
        finish_level="medio",
        insulation="Panel sandwich 75 mm",
        warranty_years=5,
        lead_time_weeks=10,
        reference_url=AMAZON_REFERENCE_URL,
        notes=(
            "Se despliega en obra en 1-2 dias. Al plegarse viaja en un solo "
            "contenedor, lo que abarata mucho el transporte."
        ),
    ),
    PrefabModel(
        id="plegable-20-1dorm",
        name="Casa plegable 20' - 1 dormitorio",
        family="plegable",
        area_m2=18.0,
        length_m=6.0,
        width_m=3.0,
        height_m=2.8,
        bedrooms=1,
        bathrooms=1,
        base_price_eur=11500.0,
        transport_eur=1500.0,
        assembly_eur=1600.0,
        needs_crane=True,
        crane_eur=700.0,
        finish_level="basico",
        insulation="Panel sandwich 50 mm",
        warranty_years=3,
        lead_time_weeks=8,
        reference_url=AMAZON_REFERENCE_URL,
        notes="Entrada mas barata. Como alojamiento turistico limita el aforo a 2-3 personas.",
    ),
    PrefabModel(
        id="expandible-40-premium",
        name="Modulo expandible 40' - 2 dormitorios, acabado alto",
        family="expandible",
        area_m2=40.0,
        length_m=12.2,
        width_m=3.3,
        height_m=2.9,
        bedrooms=2,
        bathrooms=1,
        base_price_eur=29500.0,
        transport_eur=2400.0,
        assembly_eur=3200.0,
        needs_crane=True,
        crane_eur=950.0,
        finish_level="alto",
        insulation="Panel sandwich 100 mm + rotura de puente termico",
        warranty_years=10,
        lead_time_weeks=14,
        reference_url=AMAZON_REFERENCE_URL,
        notes="Mejor aislamiento y acabados: sostiene una tarifa por noche mas alta.",
    ),
    PrefabModel(
        id="modular-60",
        name="Vivienda modular 60 m2 - 2 dormitorios",
        family="modular",
        area_m2=60.0,
        length_m=10.0,
        width_m=6.0,
        height_m=3.0,
        bedrooms=2,
        bathrooms=2,
        base_price_eur=66000.0,
        transport_eur=3500.0,
        assembly_eur=6500.0,
        needs_crane=True,
        crane_eur=1400.0,
        finish_level="alto",
        insulation="Estructura ligera de acero, SATE",
        warranty_years=10,
        lead_time_weeks=20,
        reference_url="",
        notes="Cumple CTE de forma holgada; la via mas comoda para obtener cedula de habitabilidad.",
    ),
    PrefabModel(
        id="modular-90",
        name="Vivienda modular 90 m2 - 3 dormitorios",
        family="modular",
        area_m2=90.0,
        length_m=15.0,
        width_m=6.0,
        height_m=3.0,
        bedrooms=3,
        bathrooms=2,
        base_price_eur=96000.0,
        transport_eur=4500.0,
        assembly_eur=9000.0,
        needs_crane=True,
        crane_eur=1800.0,
        finish_level="alto",
        insulation="Estructura ligera de acero, SATE",
        warranty_years=10,
        lead_time_weeks=24,
        reference_url="",
        notes="Mas aforo y por tanto mas ingreso por noche, pero sube mucho la inversion inicial.",
    ),
]

CATALOG_BY_ID: dict[str, PrefabModel] = {model.id: model for model in CATALOG}
DEFAULT_MODEL_ID = "plegable-40-2dorm"


def get_model(model_id: str) -> PrefabModel:
    try:
        return CATALOG_BY_ID[model_id]
    except KeyError:
        raise ValueError(
            f"Modelo '{model_id}' desconocido. Disponibles: {', '.join(CATALOG_BY_ID)}"
        ) from None


def list_models() -> list[dict]:
    return [model.as_dict() for model in CATALOG]
