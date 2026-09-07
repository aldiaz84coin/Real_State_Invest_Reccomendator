"""Tests del simulador: costes, plan de negocio e implantacion."""
import pytest

from app.simulation.business_plan import (
    RentalAssumptions, annual_loan_payment, build_business_plan, irr, npv,
)
from app.simulation.catalog import CATALOG, get_model, list_models
from app.simulation.costs import CostAssumptions, compute_investment
from app.simulation.render2d import render_site_plan_svg
from app.simulation.siteplan import SitePlanOptions, build_site_plan, synthetic_parcel


class TestCatalog:
    def test_todos_los_modelos_son_coherentes(self):
        for model in CATALOG:
            assert model.area_m2 > 0
            assert model.turnkey_module_eur > model.base_price_eur
            assert model.length_m * model.width_m >= model.area_m2 * 0.9
            assert model.bedrooms >= 1

    def test_modelo_de_referencia_presente(self):
        model = get_model("plegable-40-2dorm")
        assert "amazon" in model.reference_url.lower()
        assert model.bedrooms == 2

    def test_modelo_desconocido(self):
        with pytest.raises(ValueError, match="desconocido"):
            get_model("no-existe")

    def test_listado_serializa(self):
        models = list_models()
        assert all("turnkey_module_eur" in m for m in models)


class TestCosts:
    def test_desglose_completo(self):
        breakdown = compute_investment(
            land_price_eur=45000, parcel_area_m2=900, model="plegable-40-2dorm"
        )
        categories = breakdown.by_category()
        for expected in ("compra", "impuestos", "vivienda", "obra",
                         "acometidas", "licencias", "equipamiento", "contingencia"):
            assert expected in categories, f"falta la categoria {expected}"
        assert breakdown.total_eur > 45000

    def test_el_total_cuadra_con_las_lineas(self):
        breakdown = compute_investment(
            land_price_eur=60000, parcel_area_m2=1200, model="modular-60"
        )
        assert sum(l.amount_eur for l in breakdown.lines) == pytest.approx(
            breakdown.total_eur, rel=1e-9
        )

    def test_itp_depende_de_la_comunidad(self):
        def impuestos(ccaa):
            breakdown = compute_investment(
                land_price_eur=100000, parcel_area_m2=1000, model="plegable-40-2dorm",
                assumptions=CostAssumptions(ccaa=ccaa),
            )
            return next(l.amount_eur for l in breakdown.lines if l.concept.startswith("ITP"))

        # Pais Vasco (4%) frente a Cataluna (10%).
        assert impuestos("Pais Vasco") == pytest.approx(4000.0)
        assert impuestos("Cataluna") == pytest.approx(10000.0)

    def test_vendedor_empresa_paga_iva_no_itp(self):
        breakdown = compute_investment(
            land_price_eur=100000, parcel_area_m2=1000, model="plegable-40-2dorm",
            assumptions=CostAssumptions(ccaa="Madrid", seller_is_business=True),
        )
        conceptos = [l.concept for l in breakdown.lines]
        assert any("IVA" in c for c in conceptos)
        assert not any(c.startswith("ITP") for c in conceptos)

    def test_el_pem_excluye_impuestos_y_honorarios(self):
        breakdown = compute_investment(
            land_price_eur=50000, parcel_area_m2=1000, model="plegable-40-2dorm"
        )
        # El PEM es solo obra fisica: nunca puede incluir el suelo.
        assert breakdown.pem_eur < breakdown.total_eur
        assert breakdown.pem_eur < 50000

    def test_solar_aislada_sustituye_la_acometida(self):
        assumptions = CostAssumptions()
        assumptions.electricity_connection_eur = 0.0
        assumptions.off_grid_solar_eur = 14500.0
        breakdown = compute_investment(
            land_price_eur=40000, parcel_area_m2=1000,
            model="plegable-40-2dorm", assumptions=assumptions,
        )
        conceptos = [l.concept for l in breakdown.lines]
        assert "Instalacion solar aislada" in conceptos
        assert "Acometida electrica" not in conceptos

    def test_imprevistos_escalan_el_total(self):
        base = compute_investment(land_price_eur=50000, parcel_area_m2=1000,
                                  model="plegable-40-2dorm",
                                  assumptions=CostAssumptions(contingency_rate=0.0))
        con_margen = compute_investment(land_price_eur=50000, parcel_area_m2=1000,
                                        model="plegable-40-2dorm",
                                        assumptions=CostAssumptions(contingency_rate=0.20))
        assert con_margen.total_eur == pytest.approx(base.total_eur * 1.20, rel=1e-6)


class TestFinance:
    def test_cuota_de_prestamo(self):
        # 100.000 € al 5% a 20 anos rondan los 8.024 € al ano.
        assert annual_loan_payment(100_000, 0.05, 20) == pytest.approx(8024.3, abs=2)

    def test_prestamo_sin_interes(self):
        assert annual_loan_payment(120_000, 0.0, 10) == pytest.approx(12_000.0)

    def test_van_a_tipo_cero_es_la_suma(self):
        assert npv(0.0, [-100, 50, 50, 50]) == pytest.approx(50.0)

    def test_tir_conocida(self):
        # Invertir 1000 y recibir 1100 en un ano es exactamente un 10%.
        assert irr([-1000, 1100]) == pytest.approx(0.10, abs=1e-4)

    def test_tir_inexistente_devuelve_none(self):
        assert irr([100, 200, 300]) is None
        assert irr([]) is None

    def test_van_cero_en_la_tir(self):
        flows = [-50_000, 8_000, 8_500, 9_000, 9_500, 30_000]
        rate = irr(flows)
        assert rate is not None
        assert npv(rate, flows) == pytest.approx(0.0, abs=1e-3)


class TestBusinessPlan:
    @staticmethod
    def _plan(**kwargs):
        defaults = dict(adr_eur=120.0, occupancy_rate=0.55, zone_type="costa")
        defaults.update(kwargs)
        return build_business_plan(
            investment_eur=140_000, area_m2=36, bedrooms=2,
            assumptions=RentalAssumptions(**defaults),
        )

    def test_ingresos_coinciden_con_tarifa_por_noches(self):
        plan = self._plan()
        esperado = 120.0 * 365 * 0.55
        assert plan.revenue["gross_revenue_eur"] == pytest.approx(esperado, rel=1e-6)

    def test_prima_de_calidad_sube_la_tarifa(self):
        base = self._plan()
        premium = self._plan(quality_premium=0.20)
        assert premium.revenue["adr_eur"] == pytest.approx(base.revenue["adr_eur"] * 1.20)

    def test_la_proyeccion_tiene_el_horizonte_pedido(self):
        plan = self._plan(horizon_years=15)
        assert len(plan.projection) == 15
        assert plan.projection[-1]["year"] == 15

    def test_el_primer_ano_arranca_por_debajo(self):
        plan = self._plan(ramp_up_year_1=0.7)
        assert plan.projection[0]["gross_revenue"] < plan.projection[1]["gross_revenue"]

    def test_el_prestamo_se_amortiza_del_todo(self):
        plan = self._plan(loan_amount_eur=90_000, loan_rate=0.045, loan_years=10,
                          horizon_years=10)
        assert plan.projection[-1]["loan_outstanding"] == pytest.approx(0.0, abs=1.0)

    def test_el_flujo_acumulado_es_monotono_si_hay_beneficio(self):
        plan = self._plan()
        acumulados = [y["accumulated_cash_flow"] for y in plan.projection]
        assert acumulados == sorted(acumulados)

    def test_doce_meses_de_estacionalidad(self):
        plan = self._plan()
        assert len(plan.monthly) == 12
        # En la costa, agosto tiene que superar a enero.
        agosto = next(m for m in plan.monthly if m["month"] == "Ago")
        enero = next(m for m in plan.monthly if m["month"] == "Ene")
        assert agosto["revenue"] > enero["revenue"] * 2

    def test_la_montana_invierte_la_temporada(self):
        plan = self._plan(zone_type="montana")
        enero = next(m for m in plan.monthly if m["month"] == "Ene")
        mayo = next(m for m in plan.monthly if m["month"] == "May")
        assert enero["revenue"] > mayo["revenue"]

    def test_ocupacion_baja_genera_aviso(self):
        plan = self._plan(occupancy_rate=0.12)
        assert any("baja" in w for w in plan.warnings)

    def test_siempre_avisa_de_la_licencia_turistica(self):
        assert any("uso turistico" in w for w in self._plan().warnings)

    def test_ocupacion_se_limita_a_un_maximo_realista(self):
        plan = self._plan(occupancy_rate=0.99)
        assert plan.revenue["occupancy_rate"] <= 0.95


class TestSitePlan:
    def test_implantacion_en_parcela_holgada(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(1200, 36.72, -4.42),
            model="plegable-40-2dorm",
            options=SitePlanOptions(include_pool=True),
        )
        assert plan.feasible
        assert plan.house["area_m2"] == pytest.approx(36.0)
        assert plan.terrace and plan.parking and plan.pool
        assert plan.compliance["min_clearance_m"] > 0

    def test_la_casa_cabe_dentro_de_lo_edificable(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(1000, 36.72, -4.42),
            model="modular-60",
        )
        assert plan.metrics["house_footprint_m2"] <= plan.metrics["buildable_area_m2"]

    def test_parcela_insuficiente_no_es_viable(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(200, 36.72, -4.42), model="modular-90"
        )
        assert not plan.feasible
        assert plan.warnings

    def test_retranqueos_mayores_reducen_lo_edificable(self):
        ring = synthetic_parcel(1500, 36.72, -4.42)
        holgado = build_site_plan(parcel_ring_lonlat=ring, model="plegable-40-2dorm",
                                  options=SitePlanOptions(setback_front_m=3, setback_sides_m=3))
        estricto = build_site_plan(parcel_ring_lonlat=ring, model="plegable-40-2dorm",
                                   options=SitePlanOptions(setback_front_m=10, setback_sides_m=10))
        assert estricto.metrics["buildable_area_m2"] < holgado.metrics["buildable_area_m2"]

    def test_ocupacion_excesiva_se_detecta(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(260, 36.72, -4.42),
            model="plegable-20-1dorm",
            options=SitePlanOptions(setback_front_m=1, setback_sides_m=1,
                                    max_occupancy_rate=0.05),
        )
        if plan.feasible:
            assert not plan.compliance["occupancy_ok"]

    def test_geometria_disponible_en_metros_y_en_lonlat(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(900, 36.72, -4.42),
            model="plegable-40-2dorm",
        )
        assert len(plan.parcel["meters"]) == len(plan.parcel["lonlat"])
        # El lon/lat debe caer cerca del punto de origen pedido.
        lon, lat = plan.parcel["lonlat"][0]
        assert abs(lat - 36.72) < 0.01 and abs(lon + 4.42) < 0.01

    def test_la_superficie_declarada_se_respeta(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(900, 36.72, -4.42),
            model="plegable-40-2dorm",
        )
        assert plan.metrics["parcel_area_m2"] == pytest.approx(900, rel=0.02)

    def test_svg_valido(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(900, 36.72, -4.42),
            model="plegable-40-2dorm",
        )
        svg = render_site_plan_svg(plan.as_dict())
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "m2" in svg  # la leyenda con superficies
        assert " m<" in svg or "m</text>" in svg  # cotas de los lados

    def test_svg_sin_geometria_no_revienta(self):
        svg = render_site_plan_svg({"parcel": {"meters": []}})
        assert "Sin geometria" in svg
