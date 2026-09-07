"""Coste total de la inversion: compra, obra, acometidas, licencias e impuestos.

Los tipos impositivos y los precios unitarios son los vigentes de referencia en
2026 y estan todos parametrizados: la fiscalidad inmobiliaria es autonomica y
las tasas municipales varian mucho, asi que la simulacion permite ajustarlos.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.simulation.catalog import PrefabModel, get_model

# ITP en transmision entre particulares (tipo general de vivienda/suelo usado).
# Fuente: normativa autonomica vigente 2026. Se puede sobreescribir.
ITP_BY_CCAA: dict[str, float] = {
    "Andalucia": 0.070,
    "Aragon": 0.080,
    "Asturias": 0.080,
    "Baleares": 0.080,
    "Canarias": 0.065,
    "Cantabria": 0.090,
    "Castilla-La Mancha": 0.090,
    "Castilla y Leon": 0.080,
    "Cataluna": 0.100,
    "Ceuta": 0.060,
    "Comunidad Valenciana": 0.100,
    "Extremadura": 0.080,
    "Galicia": 0.080,
    "La Rioja": 0.070,
    "Madrid": 0.060,
    "Melilla": 0.060,
    "Murcia": 0.080,
    "Navarra": 0.060,
    "Pais Vasco": 0.040,
}
DEFAULT_ITP_RATE = 0.080

# Actos Juridicos Documentados, aplicable a la compra con IVA y a la obra nueva.
AJD_BY_CCAA: dict[str, float] = {
    "Andalucia": 0.012,
    "Aragon": 0.015,
    "Asturias": 0.012,
    "Baleares": 0.015,
    "Canarias": 0.010,
    "Cantabria": 0.015,
    "Castilla-La Mancha": 0.015,
    "Castilla y Leon": 0.015,
    "Cataluna": 0.015,
    "Comunidad Valenciana": 0.015,
    "Extremadura": 0.015,
    "Galicia": 0.015,
    "La Rioja": 0.010,
    "Madrid": 0.0075,
    "Murcia": 0.020,
    "Navarra": 0.005,
    "Pais Vasco": 0.005,
}
DEFAULT_AJD_RATE = 0.015

IVA_SUELO = 0.21          # Solo si el vendedor es empresario o promotor.
IVA_OBRA_NUEVA = 0.10     # Ejecucion de obra de vivienda.
IVA_GENERAL = 0.21


@dataclass
class CostAssumptions:
    """Hipotesis economicas de la simulacion. Todas editables por el usuario."""

    ccaa: str = "Andalucia"
    seller_is_business: bool = False   # True -> IVA + AJD en vez de ITP

    # Compra del terreno
    notary_eur: float = 900.0
    land_registry_eur: float = 650.0
    gestoria_eur: float = 400.0
    survey_eur: float = 800.0          # levantamiento topografico
    geotechnical_eur: float = 1200.0   # estudio geotecnico (exigido por el CTE)
    legal_check_eur: float = 600.0     # nota simple, cargas, urbanistico

    # Preparacion de la parcela
    earthworks_eur_m2: float = 18.0    # desbroce y nivelacion
    prepared_area_m2: float | None = None   # por defecto, huella + 60%
    foundation_eur_m2: float = 115.0   # losa de hormigon armado
    access_road_eur: float = 2500.0
    fencing_eur_ml: float = 45.0
    fencing_perimeter_m: float = 0.0

    # Acometidas
    water_connection_eur: float = 2200.0
    electricity_connection_eur: float = 3500.0
    sewage_or_septic_eur: float = 3800.0   # fosa septica homologada
    off_grid_solar_eur: float = 0.0        # alternativa si no hay red
    telecom_eur: float = 400.0

    # Proyecto y licencias
    technical_project_pct: float = 0.08    # honorarios tecnicos sobre PEM
    icio_rate: float = 0.035               # 2%-4% segun municipio
    building_permit_rate: float = 0.010    # tasa de licencia, 0,5%-1,5%
    visado_rate: float = 0.005
    new_build_declaration_eur: float = 1400.0   # notaria + registro obra nueva
    first_occupation_eur: float = 450.0
    habitability_cert_eur: float = 350.0
    tourist_licence_eur: float = 300.0     # alta de vivienda de uso turistico

    # Equipamiento
    furnishing_eur_m2: float = 380.0
    outdoor_eur: float = 4500.0            # terraza, pergola, mobiliario exterior
    pool_eur: float = 0.0

    # Margen de seguridad
    contingency_rate: float = 0.10

    @property
    def itp_rate(self) -> float:
        return ITP_BY_CCAA.get(self.ccaa, DEFAULT_ITP_RATE)

    @property
    def ajd_rate(self) -> float:
        return AJD_BY_CCAA.get(self.ccaa, DEFAULT_AJD_RATE)


@dataclass
class CostLine:
    concept: str
    amount_eur: float
    category: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "concept": self.concept,
            "amount_eur": round(self.amount_eur, 2),
            "category": self.category,
            "detail": self.detail,
        }


@dataclass
class InvestmentBreakdown:
    lines: list[CostLine] = field(default_factory=list)
    land_price_eur: float = 0.0
    total_eur: float = 0.0
    pem_eur: float = 0.0

    def by_category(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for line in self.lines:
            totals[line.category] = round(totals.get(line.category, 0.0) + line.amount_eur, 2)
        return totals

    def as_dict(self) -> dict:
        return {
            "lines": [line.as_dict() for line in self.lines],
            "by_category": self.by_category(),
            "land_price_eur": round(self.land_price_eur, 2),
            "pem_eur": round(self.pem_eur, 2),
            "total_eur": round(self.total_eur, 2),
        }


def compute_investment(
    *,
    land_price_eur: float,
    parcel_area_m2: float,
    model: PrefabModel | str,
    assumptions: CostAssumptions | None = None,
) -> InvestmentBreakdown:
    """Calcula el desembolso total hasta tener la casa lista para alquilar."""
    if isinstance(model, str):
        model = get_model(model)
    a = assumptions or CostAssumptions()

    lines: list[CostLine] = []
    add = lines.append

    # --- 1. Compra del terreno ------------------------------------------
    add(CostLine("Precio del terreno", land_price_eur, "compra",
                 f"{parcel_area_m2:.0f} m2 a {land_price_eur / parcel_area_m2:.2f} €/m2"
                 if parcel_area_m2 else ""))

    if a.seller_is_business:
        # Vendedor empresario: la operacion va con IVA y ademas AJD.
        add(CostLine("IVA de la compra (21%)", land_price_eur * IVA_SUELO, "impuestos",
                     "Aplica cuando el vendedor es empresario o promotor."))
        add(CostLine(f"AJD ({a.ajd_rate * 100:.2f}%)", land_price_eur * a.ajd_rate, "impuestos",
                     f"Tipo de {a.ccaa}."))
    else:
        add(CostLine(f"ITP ({a.itp_rate * 100:.1f}%)", land_price_eur * a.itp_rate, "impuestos",
                     f"Tipo general de {a.ccaa}. La base es el mayor entre precio y "
                     "valor de referencia de Catastro."))

    add(CostLine("Notaria de compraventa", a.notary_eur, "compra"))
    add(CostLine("Registro de la Propiedad", a.land_registry_eur, "compra"))
    add(CostLine("Gestoria", a.gestoria_eur, "compra"))
    add(CostLine("Comprobacion juridica y urbanistica", a.legal_check_eur, "compra",
                 "Nota simple, cargas, certificado de compatibilidad urbanistica."))
    add(CostLine("Levantamiento topografico", a.survey_eur, "tecnicos"))
    add(CostLine("Estudio geotecnico", a.geotechnical_eur, "tecnicos",
                 "Exigido por el CTE para dimensionar la cimentacion."))

    # --- 2. Vivienda prefabricada ---------------------------------------
    add(CostLine(f"Modulo: {model.name}", model.base_price_eur, "vivienda",
                 f"{model.area_m2:.0f} m2, {model.bedrooms} dorm, acabado {model.finish_level}."))
    add(CostLine("Transporte a parcela", model.transport_eur, "vivienda"))
    add(CostLine("Montaje y despliegue", model.assembly_eur, "vivienda"))
    if model.needs_crane:
        add(CostLine("Grua de descarga", model.crane_eur, "vivienda"))

    # --- 3. Obra de parcela ---------------------------------------------
    footprint = model.length_m * model.width_m
    prepared = a.prepared_area_m2 if a.prepared_area_m2 else footprint * 1.6
    prepared = min(prepared, parcel_area_m2) if parcel_area_m2 else prepared

    add(CostLine("Desbroce y nivelacion", prepared * a.earthworks_eur_m2, "obra",
                 f"{prepared:.0f} m2 a {a.earthworks_eur_m2:.0f} €/m2."))
    add(CostLine("Cimentacion (losa)", footprint * a.foundation_eur_m2, "obra",
                 f"{footprint:.0f} m2 de huella a {a.foundation_eur_m2:.0f} €/m2."))
    add(CostLine("Acceso rodado", a.access_road_eur, "obra"))
    if a.fencing_perimeter_m > 0:
        add(CostLine("Vallado", a.fencing_perimeter_m * a.fencing_eur_ml, "obra",
                     f"{a.fencing_perimeter_m:.0f} ml a {a.fencing_eur_ml:.0f} €/ml."))

    # --- 4. Acometidas ---------------------------------------------------
    add(CostLine("Acometida de agua", a.water_connection_eur, "acometidas"))
    if a.off_grid_solar_eur > 0:
        add(CostLine("Instalacion solar aislada", a.off_grid_solar_eur, "acometidas",
                     "Alternativa cuando no hay red electrica cercana."))
    else:
        add(CostLine("Acometida electrica", a.electricity_connection_eur, "acometidas"))
    add(CostLine("Saneamiento / fosa septica", a.sewage_or_septic_eur, "acometidas"))
    add(CostLine("Telecomunicaciones", a.telecom_eur, "acometidas",
                 "Imprescindible: sin buen wifi el alquiler turistico pierde reservas."))

    # --- 5. Proyecto, licencias e impuestos de obra ----------------------
    # El PEM (presupuesto de ejecucion material) es la base de ICIO, tasas y
    # visado. Se toma el coste fisico de la obra, sin honorarios ni impuestos.
    pem = sum(
        line.amount_eur for line in lines if line.category in ("vivienda", "obra", "acometidas")
    )

    add(CostLine("Proyecto tecnico y direccion de obra",
                 pem * a.technical_project_pct, "licencias",
                 f"{a.technical_project_pct * 100:.0f}% del PEM ({pem:,.0f} €)."))
    add(CostLine(f"ICIO ({a.icio_rate * 100:.1f}%)", pem * a.icio_rate, "licencias",
                 "Impuesto municipal sobre construcciones. Varia del 2% al 4%."))
    add(CostLine(f"Tasa de licencia de obra ({a.building_permit_rate * 100:.1f}%)",
                 pem * a.building_permit_rate, "licencias"))
    add(CostLine("Visado colegial", pem * a.visado_rate, "licencias"))
    add(CostLine("Declaracion de obra nueva", a.new_build_declaration_eur, "licencias",
                 "Notaria y Registro. Necesaria para inscribir la vivienda."))
    add(CostLine(f"AJD obra nueva ({a.ajd_rate * 100:.2f}%)", pem * a.ajd_rate, "licencias",
                 f"Sobre el valor declarado de la obra. Tipo de {a.ccaa}."))
    add(CostLine("Licencia de primera ocupacion", a.first_occupation_eur, "licencias"))
    add(CostLine("Cedula de habitabilidad", a.habitability_cert_eur, "licencias"))
    add(CostLine("Alta de vivienda de uso turistico", a.tourist_licence_eur, "licencias",
                 "Registro autonomico. Comprobar que el municipio no tenga moratoria."))

    # --- 6. Equipamiento -------------------------------------------------
    add(CostLine("Amueblado y equipamiento", model.area_m2 * a.furnishing_eur_m2, "equipamiento",
                 f"{model.area_m2:.0f} m2 a {a.furnishing_eur_m2:.0f} €/m2, nivel alquiler turistico."))
    add(CostLine("Exteriores (terraza, pergola, mobiliario)", a.outdoor_eur, "equipamiento"))
    if a.pool_eur > 0:
        add(CostLine("Piscina", a.pool_eur, "equipamiento"))

    # --- 7. Imprevistos --------------------------------------------------
    subtotal = sum(line.amount_eur for line in lines)
    add(CostLine(f"Imprevistos ({a.contingency_rate * 100:.0f}%)",
                 subtotal * a.contingency_rate, "contingencia",
                 "Sobrecostes de obra, retrasos y ajustes de proyecto."))

    total = subtotal * (1 + a.contingency_rate)
    return InvestmentBreakdown(
        lines=lines, land_price_eur=land_price_eur, total_eur=total, pem_eur=pem
    )
