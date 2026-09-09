"""Tests del motor de analisis: geometria, tendencia, mercado y puntuacion."""
import math

import pytest

from app.analysis.geo import (
    LocalProjection, haversine_km, point_in_polygon, polygon_area,
    rectangle, shrink_polygon,
)
from app.analysis.market import Comparable, compare_to_market, undervaluation_score
from app.analysis.poi import LocationAnalysis, PoiRef, analyze_location, location_score, size_score
from app.analysis.scoring import score_opportunity
from app.analysis.trend import analyze_series, compute_cagr, linear_regression, trend_score


class TestGeo:
    def test_haversine_conocida(self):
        # Madrid - Barcelona son unos 505 km en linea recta.
        distance = haversine_km(40.4168, -3.7038, 41.3874, 2.1686)
        assert 495 < distance < 515

    def test_proyeccion_ida_y_vuelta(self):
        projection = LocalProjection(36.72, -4.42)
        x, y = projection.to_meters(-4.41, 36.73)
        lon, lat = projection.to_lonlat(x, y)
        assert lon == pytest.approx(-4.41, abs=1e-9)
        assert lat == pytest.approx(36.73, abs=1e-9)

    def test_area_rectangulo(self):
        assert polygon_area(rectangle(0, 0, 20, 10, 0)) == pytest.approx(200.0)
        # El area no depende del giro.
        assert polygon_area(rectangle(0, 0, 20, 10, 37)) == pytest.approx(200.0, rel=1e-9)

    def test_retranqueo_reduce_lo_esperado(self):
        # Un cuadrado de 30x30 retranqueado 5 m debe quedar en 20x20.
        shrunk = shrink_polygon(rectangle(0, 0, 30, 30, 0), 5.0)
        assert polygon_area(shrunk) == pytest.approx(400.0, rel=1e-6)

    def test_retranqueo_excesivo_anula_la_parcela(self):
        assert shrink_polygon(rectangle(0, 0, 8, 8, 0), 5.0) == []

    def test_punto_en_poligono(self):
        square = rectangle(0, 0, 10, 10, 0)
        assert point_in_polygon((0, 0), square)
        assert not point_in_polygon((20, 0), square)


class TestTrend:
    def test_regresion_lineal_perfecta(self):
        slope, intercept, r2 = linear_regression([1, 2, 3, 4], [2, 4, 6, 8])
        assert slope == pytest.approx(2.0)
        assert intercept == pytest.approx(0.0, abs=1e-9)
        assert r2 == pytest.approx(1.0)

    def test_cagr(self):
        # Duplicar en 10 anos es aproximadamente un 7,18% anual.
        assert compute_cagr(100, 200, 10) == pytest.approx(0.0718, abs=1e-3)

    def test_cagr_invalido(self):
        assert compute_cagr(0, 200, 10) is None
        assert compute_cagr(100, 200, 0) is None

    def test_serie_ascendente(self):
        result = analyze_series([(2019, 100), (2020, 108), (2021, 117), (2022, 126), (2023, 137)])
        assert result.is_rising
        assert result.cagr == pytest.approx(0.0808, abs=5e-3)
        assert result.r_squared > 0.98
        assert result.confidence == "alta"

    def test_serie_descendente_no_puntua(self):
        result = analyze_series([(2019, 140), (2021, 120), (2023, 100)])
        assert not result.is_rising
        assert trend_score(result) == 0.0

    def test_serie_insuficiente(self):
        assert analyze_series([]).confidence == "insuficiente"
        assert analyze_series([(2020, 100)]).confidence == "insuficiente"

    def test_crecimiento_desbocado_no_puntua_mas_que_el_sostenido(self):
        sostenido = analyze_series([(2018, 100), (2019, 108), (2020, 117),
                                    (2021, 126), (2022, 136), (2023, 147)])
        desbocado = analyze_series([(2018, 100), (2019, 105), (2020, 110),
                                    (2021, 115), (2022, 120), (2023, 320)])
        assert trend_score(sostenido) > trend_score(desbocado)

    def test_valores_anuales_repetidos_se_promedian(self):
        result = analyze_series([(2020, 90), (2020, 110), (2023, 150)])
        assert result.first_value == pytest.approx(100.0)


class TestMarket:
    @staticmethod
    def _universe(prices, lat=36.72, lon=-4.42, area=1000):
        return [Comparable(p, lat, lon, area) for p in prices]

    def test_descuento_sobre_la_mediana(self):
        target = Comparable(50.0, 36.72, -4.42, 1000)
        comparison = compare_to_market(target, self._universe([90, 100, 110, 100, 95, 105, 98, 102]))
        assert comparison.median_eur_m2 == pytest.approx(100.0)
        assert comparison.discount == pytest.approx(0.5)
        assert comparison.reliable

    def test_la_mediana_resiste_a_los_valores_extremos(self):
        target = Comparable(50.0, 36.72, -4.42, 1000)
        # Un unico anuncio de lujo dispara la media pero no la mediana.
        comparison = compare_to_market(target, self._universe([90, 100, 110, 100, 95, 105, 98, 5000]))
        assert comparison.median_eur_m2 == pytest.approx(100.0, rel=0.05)
        assert comparison.mean_eur_m2 > 600

    def test_descarta_comparables_de_otro_tamano(self):
        target = Comparable(50.0, 36.72, -4.42, 1000)
        lejano_en_tamano = [Comparable(200.0, 36.72, -4.42, 100_000) for _ in range(10)]
        comparison = compare_to_market(target, lejano_en_tamano)
        assert comparison.sample_size == 0

    def test_descarta_comparables_lejanos(self):
        target = Comparable(50.0, 36.72, -4.42, 1000)
        lejanos = [Comparable(200.0, 41.38, 2.16, 1000) for _ in range(10)]
        assert compare_to_market(target, lejanos, radius_km=15).sample_size == 0

    def test_respaldo_con_serie_oficial(self):
        target = Comparable(60.0, 36.72, -4.42, 1000)
        comparison = compare_to_market(target, [], fallback_median_eur_m2=120.0)
        assert comparison.discount == pytest.approx(0.5)
        assert not comparison.reliable

    def test_puntuacion_de_infravaloracion_es_monotona(self):
        scores = []
        for discount in (0.10, 0.25, 0.40, 0.55):
            target = Comparable(100 * (1 - discount), 36.72, -4.42, 1000)
            comparison = compare_to_market(target, self._universe([100] * 12))
            scores.append(undervaluation_score(comparison))
        assert scores == sorted(scores)
        assert scores[0] < 30 < scores[-1]

    def test_precio_por_encima_de_mercado_no_puntua(self):
        target = Comparable(150.0, 36.72, -4.42, 1000)
        comparison = compare_to_market(target, self._universe([100] * 12))
        assert comparison.discount < 0
        assert undervaluation_score(comparison) == 0.0


class TestLocation:
    def test_distancias_a_cada_tipo(self):
        pois = [
            PoiRef("Playa de la Misericordia", "beach", 36.71, -4.44),
            PoiRef("Pico Veleta", "mountain", 37.05, -3.36),
            PoiRef("Alcazaba", "heritage", 36.72, -4.41),
        ]
        analysis = analyze_location(36.72, -4.42, pois)
        assert analysis.dist_beach_km is not None and analysis.dist_beach_km < 3
        assert analysis.nearest_poi == "Alcazaba"

    def test_playa_cerca_puntua_mas_que_lejos(self):
        cerca = analyze_location(36.72, -4.42, [PoiRef("p", "beach", 36.725, -4.42)])
        lejos = analyze_location(36.72, -4.42, [PoiRef("p", "beach", 36.90, -4.42)])
        assert location_score(cerca) > location_score(lejos)

    def test_sin_pois_no_puntua(self):
        assert location_score(LocationAnalysis()) == 0.0

    def test_tamano_optimo_alrededor_de_mil_metros(self):
        assert size_score(1000) > size_score(300)
        assert size_score(1000) > size_score(5000)
        assert size_score(120) < 30
        assert size_score(0) == 0.0


class TestScoring:
    @staticmethod
    def _pieces(discount=0.4, rising=True, beach_km=2.0, area=1000):
        target = Comparable(100 * (1 - discount), 36.72, -4.42, area)
        universe = [Comparable(100.0, 36.72, -4.42, area) for _ in range(12)]
        comparison = compare_to_market(target, universe)
        series = ([(2019, 100), (2020, 108), (2021, 117), (2022, 126), (2023, 137)]
                  if rising else [(2019, 140), (2021, 120), (2023, 100)])
        trend = analyze_series(series)
        location = analyze_location(36.72, -4.42,
                                    [PoiRef("playa", "beach", 36.72 + beach_km / 111.0, -4.42)])
        return comparison, trend, location, area

    def test_caso_ideal_cumple(self):
        breakdown = score_opportunity(*self._pieces())
        assert breakdown.qualifies
        assert breakdown.total > 55
        assert any("por debajo" in r for r in breakdown.reasons)

    def test_sin_descuento_no_cumple(self):
        breakdown = score_opportunity(*self._pieces(discount=0.05))
        assert not breakdown.qualifies

    def test_sin_tendencia_alcista_no_cumple(self):
        breakdown = score_opportunity(*self._pieces(rising=False))
        assert not breakdown.qualifies
        assert breakdown.trend == 0.0

    def test_descuento_sospechoso_genera_aviso(self):
        breakdown = score_opportunity(*self._pieces(discount=0.7))
        assert any("inundacion" in w or "edificable" in w for w in breakdown.warnings)

    def test_parcela_diminuta_avisa(self):
        breakdown = score_opportunity(*self._pieces(area=150))
        assert any("minima" in w for w in breakdown.warnings)

    def test_total_es_la_suma_ponderada(self):
        comparison, trend, location, area = self._pieces()
        breakdown = score_opportunity(comparison, trend, location, area)
        expected = (
            breakdown.undervaluation * 0.38 + breakdown.trend * 0.27
            + breakdown.location * 0.25 + breakdown.size * 0.10
        )
        assert breakdown.total == pytest.approx(expected, abs=0.01)


class TestProvincias:
    """El selector salía vacío y el filtro por provincia no cruzaba nombres."""

    def test_estan_las_cincuenta_y_dos(self):
        from app.provinces import names

        assert len(names()) == 52
        assert "Cantabria" in names() and "Toledo" in names()

    def test_se_reconoce_como_la_escriba_cada_fuente(self):
        from app.provinces import code_for

        assert code_for("Baleares") == code_for("Illes Balears") == "07"
        assert code_for("Vizcaya") == code_for("Bizkaia") == "48"
        assert code_for("39") == "39"
        assert code_for("Provincia inventada") is None

    def test_las_grafias_sirven_para_filtrar(self):
        """Una subasta dice «Baleares» y el selector «Illes Balears»."""
        from app.provinces import spellings

        formas = [f.lower() for f in spellings("Illes Balears")]
        assert "baleares" in formas and "illes balears" in formas

    def test_el_conector_del_boe_cubre_toda_espana(self):
        """Llevaba sólo veintidós provincias: en el resto buscaba sin filtro."""
        from app.sources.boe import BoeSubastasSource

        for provincia in ("Toledo", "Zamora", "Cuenca", "Cantabria"):
            assert BoeSubastasSource.province_code(provincia) is not None


class TestMigracionDeColumnas:
    """La base vive en un volumen de Fly: añadir un campo no puede romperla."""

    def test_una_columna_nueva_se_anade_a_una_tabla_existente(self, tmp_path):
        import sqlite3

        from sqlalchemy import create_engine, inspect, text

        ruta = tmp_path / "vieja.db"
        # Una tabla como la que quedó desplegada antes del campo nuevo.
        with sqlite3.connect(ruta) as conexion:
            conexion.execute(
                "CREATE TABLE municipalities ("
                " id INTEGER PRIMARY KEY, ine_code VARCHAR(5), name VARCHAR(120),"
                " province VARCHAR(80), ccaa VARCHAR(80), lat FLOAT, lon FLOAT)"
            )
            conexion.execute(
                "INSERT INTO municipalities (ine_code, name, province, ccaa, lat, lon)"
                " VALUES ('39047','Noja','Cantabria','Cantabria',43.48,-3.53)"
            )

        import app.db as capa

        motor_original = capa.engine
        capa.engine = create_engine(f"sqlite:///{ruta}")
        try:
            capa.init_db()
            columnas = {c["name"] for c in inspect(capa.engine).get_columns("municipalities")}
            assert "tourist_beds" in columnas and "population" in columnas
            with capa.engine.connect() as conexion:
                fila = conexion.execute(
                    text("SELECT name, tourist_beds FROM municipalities")
                ).one()
            # Y el dato que ya había sigue ahí.
            assert fila[0] == "Noja" and fila[1] is None

            capa.init_db()      # idempotente: repetirlo no debe fallar
        finally:
            capa.engine.dispose()
            capa.engine = motor_original


class TestElGuardaDeRed:
    """El propio guarda tiene que funcionar, o no protege de nada."""

    def test_una_llamada_a_internet_falla_el_test(self):
        import httpx
        import pytest as _pytest

        from tests.conftest import SalidaDeRedProhibida

        with _pytest.raises(SalidaDeRedProhibida):
            httpx.Client(timeout=1).get("https://subastas.boe.es/")
