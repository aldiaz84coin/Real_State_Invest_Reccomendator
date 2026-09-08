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


class TestFichaDeModelo:
    """La ficha se dibuja a escala desde las dimensiones reales del catálogo,
    no es una foto: debe ser correcta para todos los modelos."""

    def test_todos_los_modelos_generan_svg_valido(self):
        from app.simulation.render_model import render_model_card_svg

        for model in CATALOG:
            svg = render_model_card_svg(model)
            assert svg.startswith("<svg") and svg.endswith("</svg>")
            assert "viewBox" in svg
            assert 'role="img"' in svg and "aria-label" in svg   # accesible

    def test_la_ficha_acota_las_dimensiones_reales(self):
        from app.simulation.render_model import render_model_card_svg

        model = get_model("plegable-40-2dorm")
        svg = render_model_card_svg(model)
        assert f"{model.length_m:g} m" in svg
        assert f"{model.width_m:g} m" in svg
        assert f"{model.height_m:g} m" in svg

    def test_la_planta_refleja_dormitorios_y_banos(self):
        from app.simulation.render_model import render_model_card_svg

        una = render_model_card_svg(get_model("plegable-20-1dorm"))
        tres = render_model_card_svg(get_model("modular-90"))
        # Un modelo de 3 dormitorios dibuja más habitaciones que uno de 1.
        assert tres.count(">dorm<") > una.count(">dorm<")
        assert "2 baños" in tres
        assert "1 baño<" in una

    def test_modelo_desconocido_no_rompe_el_catalogo(self):
        with pytest.raises(ValueError):
            get_model("no-existe")


class TestParcelaCatastral:
    """La simulación debe seguir funcionando con y sin Catastro."""

    def test_sin_catastro_usa_rectangulo_equivalente(self, monkeypatch):
        from app.simulation.siteplan import synthetic_parcel
        from app.analysis.geo import polygon_area, LocalProjection

        ring = synthetic_parcel(900, 36.72, -4.42)
        projection = LocalProjection(36.72, -4.42)
        metros = projection.ring_to_meters(ring[:-1])
        assert polygon_area(metros) == pytest.approx(900, rel=0.02)

    def test_el_geojson_del_catastro_se_convierte_a_anillo(self):
        from app.services import _extract_ring

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-4.42, 36.72], [-4.419, 36.72],
                                 [-4.419, 36.721], [-4.42, 36.721], [-4.42, 36.72]]],
            },
            "properties": {"cadastral_ref": "ABC", "official_area_m2": 1000.0},
        }
        ring = _extract_ring(feature)
        assert ring is not None and len(ring) == 5
        assert ring[0] == [-4.42, 36.72]

    def test_multipolygon_tambien_se_acepta(self):
        from app.services import _extract_ring

        feature = {"geometry": {"type": "MultiPolygon", "coordinates": [[[
            [-4.42, 36.72], [-4.419, 36.72], [-4.419, 36.721], [-4.42, 36.72]]]]}}
        assert _extract_ring(feature) is not None

    def test_geometria_invalida_devuelve_none(self):
        from app.services import _extract_ring

        assert _extract_ring({}) is None
        assert _extract_ring({"geometry": {"type": "Point", "coordinates": [0, 0]}}) is None


class TestFotoDeModelo:
    """Las fotos las aporta el usuario: no se pueden descargar en este entorno
    y son material del fabricante. La app las acepta y las guarda en el
    volumen, para poder añadirlas sin volver a desplegar."""

    @pytest.fixture
    def carpeta(self, tmp_path, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("MODEL_IMAGES_DIR", str(tmp_path))
        get_settings.cache_clear()
        yield tmp_path
        get_settings.cache_clear()

    def test_sin_foto_solo_hay_esquema(self, carpeta):
        from app.simulation.catalog import image_path_for

        assert image_path_for("plegable-40-2dorm") is None

    def test_encuentra_la_foto_subida(self, carpeta):
        from app.simulation.catalog import image_path_for

        (carpeta / "plegable-40-2dorm.jpg").write_bytes(b"datos")
        assert image_path_for("plegable-40-2dorm") == carpeta / "plegable-40-2dorm.jpg"

    def test_prefiere_webp_a_jpg(self, carpeta):
        from app.simulation.catalog import image_path_for

        (carpeta / "modular-60.jpg").write_bytes(b"a")
        (carpeta / "modular-60.webp").write_bytes(b"b")
        assert image_path_for("modular-60").suffix == ".webp"

    def test_una_url_configurada_tiene_prioridad(self, carpeta, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("PREFAB_IMAGES", "modular-90=https://ejemplo/foto.jpg")
        get_settings.cache_clear()
        (carpeta / "modular-90.jpg").write_bytes(b"local")
        from app.main import _model_image_url

        assert _model_image_url("modular-90") == "https://ejemplo/foto.jpg"

    def test_sin_url_se_sirve_la_subida(self, carpeta, monkeypatch):
        from app.config import get_settings

        monkeypatch.delenv("PREFAB_IMAGES", raising=False)
        get_settings.cache_clear()
        (carpeta / "modular-90.jpg").write_bytes(b"local")
        from app.main import _model_image_url

        assert _model_image_url("modular-90") == "/api/prefab-models/modular-90/image"

    def test_solo_se_admiten_formatos_de_imagen_web(self):
        """Nada de SVG, que puede llevar scripts, ni tipos arbitrarios."""
        from app.main import ALLOWED_IMAGE_TYPES

        assert set(ALLOWED_IMAGE_TYPES) == {"image/webp", "image/jpeg", "image/png"}
        assert "image/svg+xml" not in ALLOWED_IMAGE_TYPES

    def test_el_catalogo_expone_la_foto_cuando_existe(self, carpeta, monkeypatch):
        from app.config import get_settings

        monkeypatch.delenv("PREFAB_IMAGES", raising=False)
        get_settings.cache_clear()
        (carpeta / "plegable-20-1dorm.png").write_bytes(b"x")
        from app.main import _model_cards

        tarjetas = {m["id"]: m for m in _model_cards()}
        assert tarjetas["plegable-20-1dorm"]["image_url"].endswith("/image")
        assert tarjetas["modular-60"]["image_url"] is None
        # El esquema sigue estando en ambos casos.
        assert all(m["preview_svg"].startswith("<svg") for m in tarjetas.values())


class TestPresets:
    """El simulador arrancaba pidiendo coordenadas, que es la barrera que
    impedía probarlo. Ahora parte de un punto real de Cantabria."""

    def test_el_predeterminado_esta_en_cantabria(self):
        from app.simulation.presets import DEFAULT_PRESET_ID, get_preset

        preset = get_preset(None)
        assert preset.id == DEFAULT_PRESET_ID
        assert preset.region == "Cantabria"
        assert preset.ccaa == "Cantabria"

    def test_las_coordenadas_caen_en_cantabria(self):
        from app.simulation.presets import get_preset

        preset = get_preset(None)
        # Cantabria: aproximadamente 43,0-43,6 N y 3,1-4,9 O.
        assert 43.0 < preset.lat < 43.7
        assert -5.0 < preset.lon < -3.0

    def test_un_id_desconocido_cae_al_predeterminado(self):
        from app.simulation.presets import get_preset

        assert get_preset("no-existe").region == "Cantabria"

    def test_todos_los_presets_son_coherentes(self):
        from app.simulation.presets import PRESETS

        for preset in PRESETS:
            assert preset.zone_type in ("costa", "montana", "rural")
            assert 27 < preset.lat < 44          # dentro de España
            assert -19 < preset.lon < 5
            assert preset.typical_price_eur > 0
            assert preset.typical_area_m2 > 0
            assert preset.note

    def test_hay_costa_y_montana(self):
        from app.simulation.presets import PRESETS

        tipos = {p.zone_type for p in PRESETS}
        assert "costa" in tipos and "montana" in tipos

    def test_la_ccaa_del_preset_existe_en_la_tabla_de_itp(self):
        """Si no, el simulador aplicaría el tipo por defecto sin avisar."""
        from app.simulation.costs import ITP_BY_CCAA
        from app.simulation.presets import PRESETS

        for preset in PRESETS:
            assert preset.ccaa in ITP_BY_CCAA, preset.id


class TestOrientacionDeLaImplantacion:
    """La casa no se coloca en el centro y ya está: hay criterios, y se explican."""

    def test_la_fachada_larga_busca_el_sur(self):
        """En una parcela holgada nada impide la orientación con más sol."""
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(2000, 43.4869, -3.5290),
            model="plegable-40-2dorm",
        )
        assert plan.feasible
        # Giro 0 = lado largo este-oeste = fachada larga mirando al sur.
        desviacion = min(plan.placement["angle_deg"] % 180,
                         180 - plan.placement["angle_deg"] % 180)
        assert desviacion <= 15
        assert plan.placement["criteria"]["soleamiento"] > 0.9

    def test_la_casa_se_va_al_norte_para_dejar_el_jardin_al_sur(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(2000, 43.4869, -3.5290),
            model="plegable-40-2dorm",
        )
        assert plan.placement["criteria"]["jardin_al_sur"] > 0.5

    def test_el_retranqueo_frontal_solo_afecta_al_lindero_de_acceso(self):
        """Aplicar el frontal a todo el perímetro dejaba parcelas sin edificable."""
        ring = synthetic_parcel(600, 43.4869, -3.5290)
        plan = build_site_plan(
            parcel_ring_lonlat=ring, model="plegable-40-2dorm",
            options=SitePlanOptions(setback_front_m=8, setback_sides_m=3),
        )
        uniforme = build_site_plan(
            parcel_ring_lonlat=ring, model="plegable-40-2dorm",
            options=SitePlanOptions(setback_front_m=8, setback_sides_m=8),
        )
        assert plan.metrics["buildable_area_m2"] > uniforme.metrics["buildable_area_m2"]

    def test_la_colocacion_viene_explicada(self):
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(1500, 43.4869, -3.5290),
            model="plegable-40-2dorm",
        )
        razones = plan.placement["reasons"]
        assert razones and all(isinstance(r, str) and len(r) > 20 for r in razones)
        # Los pesos acompañan a los criterios: si no, el porcentaje no dice nada.
        assert set(plan.placement["criteria"]) == set(plan.placement["weights"])

    def test_las_vistas_giran_la_casa_cuando_no_manda_el_sol(self):
        """El sol pesa el doble que las vistas, así que sólo se nota sin él."""
        ring = synthetic_parcel(2500, 43.4869, -3.5290)
        al_este = build_site_plan(
            parcel_ring_lonlat=ring, model="plegable-40-2dorm",
            options=SitePlanOptions(orient_to_south=False, view_azimuth_deg=90.0),
        )
        assert al_este.placement["criteria"]["vistas"] > 0.9
        # Fachada mirando al este: el rectángulo gira noventa grados.
        assert 80 <= al_este.placement["angle_deg"] <= 100

    def test_el_sol_manda_sobre_las_vistas(self):
        """Una vista al norte no debe justificar una casa sin sol."""
        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(2500, 43.4869, -3.5290),
            model="plegable-40-2dorm",
            options=SitePlanOptions(view_azimuth_deg=0.0),
        )
        assert plan.placement["criteria"]["soleamiento"] > 0.9

    def test_el_plano_se_dibuja_aunque_la_casa_no_quepa(self):
        """Ocultarlo dejaba al usuario sin ver por qué no cabía."""
        from app.simulation.render2d import render_site_plan_svg

        plan = build_site_plan(
            parcel_ring_lonlat=synthetic_parcel(150, 43.4869, -3.5290),
            model="modular-90",
        )
        assert not plan.feasible
        svg = render_site_plan_svg(plan.as_dict())
        assert svg.startswith("<svg") and len(svg) > 500
