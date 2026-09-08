"""Pruebas de extremo a extremo de la aplicación.

Existen porque faltaban: la suite tenía más de doscientos tests y ninguno
llegaba a ejecutar `run_full_simulation`, que es la función central. El
resultado fue un NameError en producción por una función que se llamaba y
nunca se había definido; los tests unitarios no podían verlo porque ninguno
recorría ese camino.

La regla que fijan: cada pantalla y cada endpoint que el usuario puede tocar
se ejercita de verdad al menos una vez.
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    directory = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{directory}/test.db"
    os.environ["MODEL_IMAGES_DIR"] = f"{directory}/images"

    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import init_db
    from app.main import app

    init_db()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


class TestSimulacionCompleta:
    """El camino que recorre el usuario al pulsar «Calcular simulación»."""

    BASE = {
        "land_price_eur": 48000,
        "parcel_area_m2": 1200,
        "lat": 43.4869,          # Noja, el punto por defecto
        "lon": -3.5290,
        "model_id": "plegable-40-2dorm",
        "ccaa": "Cantabria",
        "contingency_rate": 0.10,
        "setback_front_m": 5,
        "setback_sides_m": 3,
        "max_occupancy_rate": 0.30,
        "adr_eur": 120,
        "occupancy_rate": 0.55,
        "management_fee": 0.18,
    }

    # Sin red no se puede consultar el Catastro, así que se pide el rectángulo
    # equivalente: lo que se prueba aquí es el cálculo, no la parcela. En el
    # formulario una casilla sin marcar simplemente no se envía; en JSON hay
    # que decirlo con un booleano.
    JSON = {**BASE, "use_cadastre": False}

    def test_el_formulario_devuelve_resultado_y_no_un_500(self, client):
        respuesta = client.post("/simular", data=self.BASE)
        assert respuesta.status_code == 200, respuesta.text[:500]
        assert "Inversión total" in respuesta.text
        assert "Rentabilidad neta" in respuesta.text

    def test_la_api_devuelve_la_simulacion_entera(self, client):
        respuesta = client.post("/api/simulate", json=self.JSON)
        assert respuesta.status_code == 200, respuesta.text[:500]
        datos = respuesta.json()
        for bloque in ("model", "investment", "business_plan", "site_plan", "summary"):
            assert bloque in datos, bloque
        assert datos["summary"]["total_investment_eur"] > self.BASE["land_price_eur"]
        assert datos["business_plan"]["projection"]
        assert datos["site_plan"]["svg"].startswith("<svg")

    def test_la_ficha_del_modelo_viaja_en_el_resultado(self, client):
        """Es lo que rompió en producción: la foto del modelo."""
        datos = client.post("/api/simulate", json=self.JSON).json()
        assert "preview_svg" in datos["model"]
        assert "image_url" in datos["model"]      # None si no hay foto, pero presente

    @pytest.mark.parametrize("model_id", [
        "plegable-40-2dorm", "plegable-20-1dorm", "expandible-40-premium",
        "modular-60", "modular-90",
    ])
    def test_todos_los_modelos_simulan(self, client, model_id):
        respuesta = client.post("/api/simulate", json={**self.JSON, "model_id": model_id,
                                                       "parcel_area_m2": 2000})
        assert respuesta.status_code == 200, respuesta.text[:300]

    def test_con_piscina_y_prestamo(self, client):
        respuesta = client.post("/api/simulate", json={
            **self.JSON, "include_pool": True, "pool_eur": 22000,
            "loan_amount_eur": 90000, "loan_rate": 0.045, "loan_years": 15,
        })
        assert respuesta.status_code == 200
        datos = respuesta.json()
        assert datos["business_plan"]["financing"]["loan_amount_eur"] == 90000

    def test_una_parcela_imposible_no_revienta(self, client):
        """Un módulo que no cabe debe explicarse, no romper la página."""
        respuesta = client.post("/api/simulate", json={
            **self.JSON, "parcel_area_m2": 150, "model_id": "modular-90"})
        assert respuesta.status_code == 200
        datos = respuesta.json()
        assert datos["site_plan"]["feasible"] is False
        assert datos["site_plan"]["warnings"]

    def test_faltan_datos_obligatorios(self, client):
        respuesta = client.post("/api/simulate", json={"model_id": "modular-60"})
        assert respuesta.status_code == 422

    def test_modelo_inexistente(self, client):
        respuesta = client.post("/api/simulate", json={**self.JSON, "model_id": "no-existe"})
        assert respuesta.status_code in (404, 422, 500)


class TestPaginas:
    """Toda pantalla navegable responde."""

    @pytest.mark.parametrize("ruta", ["/", "/buscar", "/simular", "/fuentes", "/health", "/docs"])
    def test_responden(self, client, ruta):
        assert client.get(ruta).status_code == 200

    def test_el_simulador_arranca_en_cantabria(self, client):
        html = client.get("/simular").text
        assert 'value="43.4869"' in html
        assert "Noja" in html

    def test_los_presets_cambian_el_punto(self, client):
        html = client.get("/simular?preset=cantabria-potes").text
        assert 'value="43.154"' in html or 'value="43.1540"' in html


class TestEndpointsDeApoyo:
    def test_catalogo_de_modelos(self, client):
        datos = client.get("/api/prefab-models").json()
        assert len(datos["models"]) == 5

    def test_presets(self, client):
        datos = client.get("/api/presets").json()
        assert datos["default"] == "cantabria-noja"
        assert any(p["region"] == "Cantabria" for p in datos["presets"])

    def test_esquema_de_un_modelo(self, client):
        respuesta = client.get("/api/prefab-models/modular-60/preview.svg")
        assert respuesta.status_code == 200
        assert respuesta.headers["content-type"].startswith("image/svg+xml")

    def test_esquema_de_un_modelo_inexistente(self, client):
        assert client.get("/api/prefab-models/nada/preview.svg").status_code == 404

    def test_oportunidades_vacias(self, client):
        datos = client.get("/api/opportunities").json()
        assert datos["count"] == 0

    def test_el_panel_de_fuentes_responde_con_la_red_caida(self, client):
        """En este entorno todo está bloqueado: debe informar, no fallar."""
        datos = client.get("/api/sources/health").json()
        assert datos["summary"]["total"] >= 11
        assert all("access" in s for s in datos["sources"])


class TestFormularioDeBusqueda:
    """El formulario de «Localizar inversión» envía sus campos numéricos
    vacíos cuando no se rellenan, y eso devolvía un error de validación en
    lugar de entenderlo como «sin límite». Los tests anteriores sólo cargaban
    la página, nunca la enviaban."""

    def test_envio_con_todos_los_campos_vacios(self, client):
        respuesta = client.get("/buscar", params={
            "q": "", "province": "", "min_area_m2": "", "max_price_eur": "",
            "min_discount_pct": "", "max_beach_km": "",
        })
        assert respuesta.status_code == 200, respuesta.text[:400]

    @pytest.mark.parametrize("campo", [
        "max_price_eur", "max_beach_km", "min_area_m2", "min_discount_pct",
    ])
    def test_cada_campo_numerico_admite_vacio(self, client, campo):
        assert client.get("/buscar", params={campo: ""}).status_code == 200

    def test_los_valores_por_defecto_siguen_aplicandose(self, client):
        """Vacío no es lo mismo que ausente: ambos deben funcionar."""
        assert client.get("/buscar").status_code == 200
        assert client.get("/buscar", params={"min_area_m2": ""}).status_code == 200

    def test_con_valores_reales(self, client):
        respuesta = client.get("/buscar", params={
            "q": "Noja", "min_area_m2": 500, "max_price_eur": 80000,
            "min_discount_pct": 25, "max_beach_km": 5,
            "require_rising_trend": "true",
        })
        assert respuesta.status_code == 200

    def test_un_numero_invalido_sigue_rechazandose(self, client):
        """Vacío significa «sin límite»; «abc» sigue siendo un error."""
        assert client.get("/buscar", params={"max_price_eur": "abc"}).status_code == 422

    def test_la_api_de_oportunidades_se_comporta_igual(self, client):
        """La API y el formulario deben tratar el vacío del mismo modo."""
        respuesta = client.get("/api/opportunities", params={
            "max_price_eur": "", "max_beach_km": "", "min_area_m2": "",
            "min_discount_pct": "", "max_area_m2": ""})
        assert respuesta.status_code == 200, respuesta.text[:300]
