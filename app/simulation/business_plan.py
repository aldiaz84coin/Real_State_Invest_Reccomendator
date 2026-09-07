"""Plan de negocio del alquiler turistico (funcionalidad 4).

Parte de tarifa y ocupacion reales de la zona (InsideAirbnb / AirROI) en lugar
de porcentajes inventados, y construye la cuenta de resultados, el retorno y
la proyeccion a diez anos.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Reparto mensual tipico de la demanda. La costa concentra mucho mas la
# temporada que el interior o la montana, y eso cambia el resultado anual.
SEASONALITY: dict[str, list[float]] = {
    #        E     F     M     A     M     J     J     A     S     O     N     D
    "costa": [0.45, 0.50, 0.62, 0.78, 0.85, 0.95, 1.45, 1.60, 1.10, 0.80, 0.50, 0.55],
    "montana": [1.35, 1.30, 1.10, 0.95, 0.75, 0.85, 1.15, 1.30, 0.80, 0.70, 0.65, 1.10],
    "ciudad": [0.80, 0.85, 1.00, 1.10, 1.10, 1.05, 1.05, 1.00, 1.10, 1.05, 0.90, 1.00],
    "rural": [0.70, 0.75, 0.90, 1.10, 1.00, 1.00, 1.35, 1.50, 1.00, 0.90, 0.75, 1.05],
}
MONTH_NAMES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
               "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


@dataclass
class RentalAssumptions:
    """Hipotesis de explotacion. Las de mercado vienen de datos reales."""

    adr_eur: float                      # tarifa media por noche en la zona
    occupancy_rate: float               # 0-1, ocupacion anual media
    zone_type: str = "costa"

    # Ajustes de la propiedad frente a la media de la zona.
    quality_premium: float = 0.0        # +0.15 = un 15% sobre la tarifa media
    ramp_up_year_1: float = 0.75        # el primer ano no se llega a ocupacion de regimen

    # Costes de explotacion
    platform_commission: float = 0.03   # comision del anfitrion en Airbnb
    channel_manager_eur_year: float = 240.0
    cleaning_cost_per_stay: float = 45.0
    avg_stay_nights: float = 4.5
    management_fee: float = 0.18        # gestion delegada; 0 si se autogestiona
    utilities_eur_month: float = 145.0
    insurance_eur_year: float = 420.0
    ibi_eur_year: float = 320.0
    maintenance_rate: float = 0.012     # sobre la inversion, anual
    supplies_per_stay: float = 12.0     # amenities y consumibles
    accounting_eur_year: float = 600.0

    # Financiacion
    loan_amount_eur: float = 0.0
    loan_rate: float = 0.045
    loan_years: int = 15

    # Fiscalidad y descuento
    tax_rate: float = 0.24              # IRPF sobre el rendimiento neto
    building_depreciation_rate: float = 0.03
    discount_rate: float = 0.07         # coste de oportunidad para el VAN
    annual_adr_growth: float = 0.02
    annual_cost_growth: float = 0.025
    horizon_years: int = 10
    terminal_value_eur: float | None = None


@dataclass
class BusinessPlan:
    revenue: dict = field(default_factory=dict)
    operating_costs: dict = field(default_factory=dict)
    profit_and_loss: dict = field(default_factory=dict)
    returns: dict = field(default_factory=dict)
    projection: list[dict] = field(default_factory=list)
    monthly: list[dict] = field(default_factory=list)
    financing: dict = field(default_factory=dict)
    assumptions: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "revenue": self.revenue,
            "operating_costs": self.operating_costs,
            "profit_and_loss": self.profit_and_loss,
            "returns": self.returns,
            "projection": self.projection,
            "monthly": self.monthly,
            "financing": self.financing,
            "assumptions": self.assumptions,
            "warnings": self.warnings,
        }


def annual_loan_payment(principal: float, rate: float, years: int) -> float:
    """Cuota anual constante de un prestamo frances."""
    if principal <= 0 or years <= 0:
        return 0.0
    if rate <= 0:
        return principal / years
    factor = (1 + rate) ** years
    return principal * rate * factor / (factor - 1)


def npv(rate: float, cashflows: list[float]) -> float:
    """Valor actual neto. cashflows[0] es el momento 0."""
    return sum(cf / ((1 + rate) ** i) for i, cf in enumerate(cashflows))


def irr(cashflows: list[float], *, low: float = -0.95, high: float = 3.0) -> float | None:
    """TIR por biseccion.

    Se prefiere a Newton-Raphson porque no diverge; si no hay cambio de signo
    en el intervalo simplemente no existe TIR real y se devuelve None.
    """
    if not cashflows or all(cf >= 0 for cf in cashflows) or all(cf <= 0 for cf in cashflows):
        return None

    f_low, f_high = npv(low, cashflows), npv(high, cashflows)
    if f_low * f_high > 0:
        return None

    # Se devuelve a precision completa: redondear aqui a seis decimales
    # dejaba un residuo apreciable en el VAN cuando los flujos son de decenas
    # de miles de euros. El redondeo es cosa de quien presenta el dato.
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid, cashflows)
        if abs(f_mid) < 1e-9:
            return mid
        if f_low * f_mid < 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2


def build_business_plan(
    *, investment_eur: float, area_m2: float, bedrooms: int, assumptions: RentalAssumptions
) -> BusinessPlan:
    """Construye el plan de negocio completo."""
    a = assumptions
    plan = BusinessPlan()
    warnings: list[str] = []

    effective_adr = a.adr_eur * (1 + a.quality_premium)
    occupancy = max(0.0, min(a.occupancy_rate, 0.95))
    occupied_nights = 365.0 * occupancy
    stays = occupied_nights / max(a.avg_stay_nights, 1.0)

    gross_revenue = effective_adr * occupied_nights
    commission = gross_revenue * a.platform_commission
    net_revenue = gross_revenue - commission

    # --- costes de explotacion ------------------------------------------
    cleaning = stays * a.cleaning_cost_per_stay
    supplies = stays * a.supplies_per_stay
    management = net_revenue * a.management_fee
    utilities = a.utilities_eur_month * 12
    maintenance = investment_eur * a.maintenance_rate

    costs = {
        "limpieza": round(cleaning, 2),
        "consumibles": round(supplies, 2),
        "gestion": round(management, 2),
        "suministros": round(utilities, 2),
        "seguro": round(a.insurance_eur_year, 2),
        "ibi": round(a.ibi_eur_year, 2),
        "mantenimiento": round(maintenance, 2),
        "asesoria": round(a.accounting_eur_year, 2),
        "channel_manager": round(a.channel_manager_eur_year, 2),
    }
    total_costs = sum(costs.values())

    ebitda = net_revenue - total_costs

    # --- financiacion ----------------------------------------------------
    loan_payment = annual_loan_payment(a.loan_amount_eur, a.loan_rate, a.loan_years)
    first_year_interest = a.loan_amount_eur * a.loan_rate
    equity = max(investment_eur - a.loan_amount_eur, 0.0)

    # --- resultado e impuestos -------------------------------------------
    # Solo se amortiza la construccion; el suelo no es amortizable.
    depreciable_base = max(investment_eur * 0.55, 0.0)
    depreciation = depreciable_base * a.building_depreciation_rate
    taxable_income = max(ebitda - first_year_interest - depreciation, 0.0)
    tax = taxable_income * a.tax_rate
    net_profit = ebitda - first_year_interest - tax
    cash_flow = ebitda - loan_payment - tax

    # --- retorno ---------------------------------------------------------
    gross_yield = gross_revenue / investment_eur if investment_eur else 0.0
    net_yield = ebitda / investment_eur if investment_eur else 0.0
    cash_on_cash = cash_flow / equity if equity else 0.0
    payback = investment_eur / ebitda if ebitda > 0 else None

    # --- proyeccion a N anos ---------------------------------------------
    projection: list[dict] = []
    cashflows: list[float] = [-equity if a.loan_amount_eur else -investment_eur]
    outstanding = a.loan_amount_eur
    accumulated = 0.0

    for year in range(1, a.horizon_years + 1):
        ramp = a.ramp_up_year_1 if year == 1 else 1.0
        growth_rev = (1 + a.annual_adr_growth) ** (year - 1)
        growth_cost = (1 + a.annual_cost_growth) ** (year - 1)

        y_gross = gross_revenue * growth_rev * ramp
        y_net_rev = y_gross * (1 - a.platform_commission)
        # Limpieza y consumibles escalan con la ocupacion; el resto, con el IPC.
        y_variable = (cleaning + supplies) * ramp * growth_cost
        y_fixed = (total_costs - cleaning - supplies) * growth_cost
        y_management = y_net_rev * a.management_fee
        y_costs = y_variable + y_fixed - management * growth_cost + y_management
        y_ebitda = y_net_rev - y_costs

        y_interest = outstanding * a.loan_rate if outstanding > 0 else 0.0
        y_principal = max(loan_payment - y_interest, 0.0) if outstanding > 0 else 0.0
        y_principal = min(y_principal, outstanding)
        outstanding = max(outstanding - y_principal, 0.0)

        y_taxable = max(y_ebitda - y_interest - depreciation, 0.0)
        y_tax = y_taxable * a.tax_rate
        y_cash = y_ebitda - y_interest - y_principal - y_tax
        accumulated += y_cash

        projection.append({
            "year": year,
            "gross_revenue": round(y_gross, 2),
            "operating_costs": round(y_costs, 2),
            "ebitda": round(y_ebitda, 2),
            "interest": round(y_interest, 2),
            "principal": round(y_principal, 2),
            "tax": round(y_tax, 2),
            "cash_flow": round(y_cash, 2),
            "accumulated_cash_flow": round(accumulated, 2),
            "loan_outstanding": round(outstanding, 2),
        })
        cashflows.append(y_cash)

    # Valor residual: por defecto la inversion revalorizada al 2% anual menos
    # la deuda viva. Es prudente y evita inflar la TIR con un supuesto agresivo.
    terminal = a.terminal_value_eur
    if terminal is None:
        terminal = investment_eur * ((1.02) ** a.horizon_years) - outstanding
    cashflows[-1] += terminal

    project_irr = irr(cashflows)
    project_npv = npv(a.discount_rate, cashflows)

    # Ano en el que el flujo acumulado cubre el desembolso inicial.
    payback_year = None
    initial_outlay = equity if a.loan_amount_eur else investment_eur
    for row in projection:
        if row["accumulated_cash_flow"] >= initial_outlay:
            payback_year = row["year"]
            break

    # --- estacionalidad mensual -------------------------------------------
    weights = SEASONALITY.get(a.zone_type, SEASONALITY["costa"])
    normalizer = sum(weights) / 12.0
    monthly = []
    for index, weight in enumerate(weights):
        month_occupancy = min(occupancy * weight / normalizer, 0.98)
        nights = 30.4 * month_occupancy
        monthly.append({
            "month": MONTH_NAMES[index],
            "occupancy": round(month_occupancy, 3),
            "nights": round(nights, 1),
            "revenue": round(nights * effective_adr, 2),
        })

    # --- avisos ------------------------------------------------------------
    if occupancy < 0.25:
        warnings.append(
            f"Ocupacion estimada baja ({occupancy * 100:.0f}%): el negocio depende mucho "
            "de la temporada alta."
        )
    if ebitda <= 0:
        warnings.append("El resultado de explotacion es negativo con estas hipotesis.")
    if payback and payback > 20:
        warnings.append(f"Periodo de recuperacion muy largo ({payback:.0f} anos).")
    if a.loan_amount_eur > 0 and cash_flow < 0:
        warnings.append("El flujo de caja no cubre la cuota del prestamo el primer ano.")
    warnings.append(
        "Verifica la normativa de vivienda de uso turistico del municipio: varias "
        "zonas tensionadas han suspendido nuevas licencias."
    )

    plan.revenue = {
        "adr_eur": round(effective_adr, 2),
        "market_adr_eur": round(a.adr_eur, 2),
        "occupancy_rate": round(occupancy, 4),
        "occupied_nights": round(occupied_nights, 1),
        "stays_per_year": round(stays, 1),
        "gross_revenue_eur": round(gross_revenue, 2),
        "platform_commission_eur": round(commission, 2),
        "net_revenue_eur": round(net_revenue, 2),
        "revenue_per_m2": round(gross_revenue / area_m2, 2) if area_m2 else 0.0,
    }
    plan.operating_costs = {**costs, "total_eur": round(total_costs, 2)}
    plan.profit_and_loss = {
        "net_revenue_eur": round(net_revenue, 2),
        "operating_costs_eur": round(total_costs, 2),
        "ebitda_eur": round(ebitda, 2),
        "ebitda_margin": round(ebitda / gross_revenue, 4) if gross_revenue else 0.0,
        "depreciation_eur": round(depreciation, 2),
        "interest_eur": round(first_year_interest, 2),
        "tax_eur": round(tax, 2),
        "net_profit_eur": round(net_profit, 2),
        "cash_flow_eur": round(cash_flow, 2),
    }
    plan.returns = {
        "investment_eur": round(investment_eur, 2),
        "equity_eur": round(equity, 2),
        "gross_yield": round(gross_yield, 4),
        "net_yield": round(net_yield, 4),
        "cash_on_cash": round(cash_on_cash, 4),
        "payback_years": round(payback, 1) if payback else None,
        "payback_year_cashflow": payback_year,
        "npv_eur": round(project_npv, 2),
        "irr": round(project_irr, 4) if project_irr is not None else None,
        "terminal_value_eur": round(terminal, 2),
    }
    plan.financing = {
        "loan_amount_eur": round(a.loan_amount_eur, 2),
        "annual_payment_eur": round(loan_payment, 2),
        "rate": a.loan_rate,
        "years": a.loan_years,
        "ltv": round(a.loan_amount_eur / investment_eur, 4) if investment_eur else 0.0,
    }
    plan.projection = projection
    plan.monthly = monthly
    plan.assumptions = {
        "zone_type": a.zone_type,
        "quality_premium": a.quality_premium,
        "management_fee": a.management_fee,
        "platform_commission": a.platform_commission,
        "avg_stay_nights": a.avg_stay_nights,
        "tax_rate": a.tax_rate,
        "discount_rate": a.discount_rate,
        "horizon_years": a.horizon_years,
        "bedrooms": bedrooms,
        "area_m2": area_m2,
    }
    plan.warnings = warnings
    return plan
