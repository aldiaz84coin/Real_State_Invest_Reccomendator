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

    def test_una_parcela_imposible_no_revienta_en_el_formulario(self, client):
        """Es el camino que falló en producción: la API devolvía el aviso pero
        la plantilla asumía que siempre hay implantación viable."""
        respuesta = client.post("/simular", data={
            **self.BASE, "parcel_area_m2": 150, "model_id": "modular-90"})
        assert respuesta.status_code == 200, respuesta.text[:600]
        assert "no cabe en esta parcela" in respuesta.text
        # Los números siguen calculándose aunque no haya plano.
        assert "Inversión total" in respuesta.text

    @pytest.mark.parametrize("area,model_id", [
        (150, "modular-90"), (200, "modular-60"), (80, "plegable-40-2dorm"),
    ])
    def test_varias_parcelas_imposibles_por_formulario(self, client, area, model_id):
        respuesta = client.post("/simular", data={
            **self.BASE, "parcel_area_m2": area, "model_id": model_id})
        assert respuesta.status_code == 200, respuesta.text[:400]

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


class TestPlanosEnLaPagina:
    """Los planos desaparecieron de la ficha al ocultarlos cuando no cabía."""

    IMPOSIBLE = {**TestSimulacionCompleta.BASE, "parcel_area_m2": 90,
                 "model_id": "modular-90"}

    def test_los_planos_salen_en_la_simulacion_normal(self, client):
        html = client.post("/simular", data=TestSimulacionCompleta.BASE).text
        assert 'id="view2d"' in html and 'id="view3d"' in html
        assert "Plano 2D" in html and "Vista 3D" in html

    def test_tambien_salen_cuando_la_casa_no_cabe(self, client):
        """La parcela y el área edificable son justo lo que explica el porqué."""
        html = client.post("/simular", data=self.IMPOSIBLE).text
        assert "no cabe en esta parcela" in html
        assert 'id="view2d"' in html and 'id="view3d"' in html
        assert "<svg" in html

    def test_la_ficha_explica_donde_se_coloca_la_casa(self, client):
        html = client.post("/simular", data=TestSimulacionCompleta.BASE).text
        assert "Por qué la casa va justo ahí" in html
        assert "Fachada larga al sur" in html


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


@pytest.fixture
def base_vacia(client):
    """Deja la base sin anuncios.

    Los tests comparten una sola base por módulo; sin esto, lo que importe
    otro test decidiría si éste pasa, según el orden en que se ejecuten.
    """
    from app.db import SessionLocal
    from app.models import Listing, OpportunityScore

    with SessionLocal() as db:
        db.query(OpportunityScore).delete()
        db.query(Listing).delete()
        db.commit()
    return client


class TestEstadoDeLosDatos:
    """Una búsqueda sin datos se veía igual que una búsqueda rota, y eso hace
    perder tiempo buscando un fallo que no existe."""

    def test_la_api_dice_si_la_base_esta_vacia(self, base_vacia):
        client = base_vacia
        estado = client.get("/api/data-status").json()
        assert estado["empty"] is True
        assert estado["listings"] == 0

    def test_el_buscador_explica_que_faltan_datos(self, base_vacia):
        html = base_vacia.get("/buscar").text
        assert "No hay ningún anuncio cargado" in html
        assert "La búsqueda funciona" in html          # separa vacío de roto
        assert "boe-subastas" in html                  # y dice cómo cargarlos

    def test_el_panel_da_los_pasos_en_orden(self, base_vacia):
        html = base_vacia.get("/").text
        assert "ingest/pois" in html
        assert "boe-subastas" in html
        assert "analyze-all" in html


class TestRegistroDeActividad:
    def test_las_peticiones_se_registran_con_su_duracion(self, client, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="investment"):
            client.get("/buscar", params={"q": "Noja"})
        mensajes = " ".join(r.getMessage() for r in caplog.records)
        assert "/buscar" in mensajes
        assert "ms" in mensajes            # la duración, que uvicorn no da
        assert "q=Noja" in mensajes        # los parámetros, para reproducirlo

    def test_las_claves_no_se_registran(self, client, caplog):
        """Un log con la clave dentro es una fuga esperando a ocurrir."""
        import logging

        with caplog.at_level(logging.INFO, logger="investment"):
            client.get("/buscar", params={"api_key": "secreto-que-no-debe-salir"})
        mensajes = " ".join(r.getMessage() for r in caplog.records)
        assert "secreto-que-no-debe-salir" not in mensajes
        assert "***" in mensajes

    def test_health_no_ensucia_el_log(self, client, caplog):
        """Fly la consulta cada pocos segundos; registrarla tapa lo demás."""
        import logging

        with caplog.at_level(logging.INFO, logger="investment"):
            client.get("/health")
        assert not [r for r in caplog.records if "/health" in r.getMessage()]


class FuenteFalsa:
    """Fuente de subastas simulada: las de verdad necesitan red."""

    key = "boe_subastas"
    name = "Subastas del BOE (inmuebles)"

    LOTES = [
        {"id_sub": "SUB-1", "price_eur": 60000.0, "area_m2": 1000.0,
         "municipality": "Noja", "province": "Cantabria"},
        {"id_sub": "SUB-2", "price_eur": 30000.0, "area_m2": 1500.0,
         "municipality": "Noja", "province": "Cantabria"},
    ]

    radius_note = "No usa radio: filtra por provincia."

    def search_attempts(self, province=None, max_results=40):
        ids = [lote["id_sub"] for lote in self.LOTES]
        return [("avanzada", ids, {"http_status": 200, "bytes": 4096})]

    def search(self, province=None, *, only_land=True, max_results=40):
        return [lote["id_sub"] for lote in self.LOTES]

    def detail(self, identifier):
        return next(l for l in self.LOTES if l["id_sub"] == identifier)

    @staticmethod
    def is_land(detail):
        return True

    @classmethod
    def normalize(cls, detail):
        return {
            "source": "boe_subastas",
            "external_id": detail["id_sub"],
            "url": f"https://ejemplo/{detail['id_sub']}",
            "title": "Finca rústica",
            "description": "",
            "price_eur": detail["price_eur"],
            "area_m2": detail["area_m2"],
            "price_eur_m2": detail["price_eur"] / detail["area_m2"],
            # Con coordenadas propias no hace falta ni Catastro ni municipio.
            "lat": 43.4869,
            "lon": -3.5290,
            "address": "",
            "municipality_name": detail["municipality"],
            "province": detail["province"],
            "land_type": "finca",
            "cadastral_ref": None,
            "raw": detail,
        }


class SumarioFalso:
    """API de sumarios simulada: devuelve los mismos lotes que el portal."""

    key = "boe_sumario"
    name = "BOE · API de sumarios (datos abiertos)"
    radius_note = "No usa radio."

    def recent_auction_ids(self, days=14, max_results=40, today=None):
        return {"ids": [lote["id_sub"] for lote in FuenteFalsa.LOTES],
                "days": [{"date": "2026-09-08", "published": True,
                          "auction_announcements": 2, "auction_ids": 2}]}


@pytest.fixture
def fuentes_simuladas(monkeypatch):
    """Sustituye las fuentes reales: sin red, y con resultados predecibles.

    Nominatim entra aquí también. Sin él, este entorno lo bloquea y los tests
    pasaban, pero en CI responde de verdad: la geocodificación de «Cantabria»
    acababa cambiando el resultado de un test que no iba de eso.
    """
    from app import discovery
    from app.sources.osm import NominatimSource

    monkeypatch.setattr(discovery, "BoeSubastasSource", FuenteFalsa)
    monkeypatch.setattr(discovery, "BoeSumarioSource", SumarioFalso)
    monkeypatch.setattr(discovery, "iter_rapidapi_sources", lambda: [])
    monkeypatch.setattr(NominatimSource, "geocode", lambda self, query: None)

    class IdealistaMudo:
        key = "idealista"
        name = "Idealista (API oficial)"

        def search_lands(self, *args, **kwargs):
            from app.sources.base import SourceError

            raise SourceError("sin credenciales")

    monkeypatch.setattr(discovery, "IdealistaSource", IdealistaMudo)


class TestDescubrimiento:
    """Buscar tiene que servir para poblar la base, no sólo para consultarla."""

    def test_descubrir_no_guarda_nada(self, client, fuentes_simuladas):
        from app.discovery import discover
        from app.db import SessionLocal
        from app.models import Listing
        from sqlalchemy import func, select

        with SessionLocal() as db:
            antes = db.scalar(select(func.count(Listing.id)))
            resultado = discover(db, province="Cantabria")
            assert db.scalar(select(func.count(Listing.id))) == antes

        assert len(resultado["candidates"]) == 2
        # Lo más barato por metro va primero: es el criterio de la aplicación.
        precios = [c["price_eur_m2"] for c in resultado["candidates"]]
        assert precios == sorted(precios)
        assert all(c["token"] for c in resultado["candidates"])

    def test_los_filtros_recortan_las_candidatas(self, client, fuentes_simuladas):
        from app.discovery import discover
        from app.db import SessionLocal

        with SessionLocal() as db:
            resultado = discover(db, province="Cantabria", max_price_eur=40000)
        assert [c["external_id"] for c in resultado["candidates"]] == ["SUB-2"]

    def test_importar_guarda_y_puntua(self, client, fuentes_simuladas):
        from app.discovery import discover, import_candidates
        from app.db import SessionLocal
        from app.models import Listing, OpportunityScore
        from sqlalchemy import select

        with SessionLocal() as db:
            candidatas = discover(db, province="Cantabria")["candidates"]
            resumen = import_candidates(db, candidatas)
            assert resumen["created"] == 2
            assert resumen["analyzed"] >= 2

            guardada = db.execute(
                select(Listing).where(Listing.external_id == "SUB-1")
            ).scalar_one()
            assert guardada.source == "boe_subastas"
            assert db.execute(
                select(OpportunityScore).where(OpportunityScore.listing_id == guardada.id)
            ).scalar_one_or_none() is not None

            # Una segunda pasada no debe duplicar ni volver a ofrecerlas.
            repetidas = discover(db, province="Cantabria")["candidates"]
            assert all(c["already_saved"] for c in repetidas)
            assert import_candidates(db, repetidas)["created"] == 0

    def test_una_candidata_no_puede_colar_columnas(self):
        """El texto vuelve del navegador: sólo se aceptan campos conocidos."""
        from app.discovery import deserialize, serialize

        empaquetada = serialize({"source": "boe_subastas", "external_id": "X",
                                 "price_eur": 1000.0, "area_m2": 100.0,
                                 "token": "abc", "already_saved": True,
                                 "id": 7, "active": False})
        recuperada = deserialize(empaquetada)
        assert "id" not in recuperada and "active" not in recuperada
        assert recuperada["external_id"] == "X"

    def test_texto_invalido_no_rompe_la_importacion(self):
        from app.discovery import deserialize

        assert deserialize("{no es json") is None
        assert deserialize("[1,2,3]") is None


class TestDescubrimientoWeb:
    """El camino que recorre el usuario desde el buscador."""

    def test_sin_provincia_la_busqueda_es_nacional(self, client, fuentes_simuladas):
        """Las Subastas del BOE buscan en toda España; exigir provincia sobraba."""
        respuesta = client.post("/buscar/descubrir", data={"q": "", "province": ""})
        assert respuesta.status_code == 200
        assert "toda España" in respuesta.text
        assert 'name="candidato"' in respuesta.text

    def test_los_campos_numericos_admiten_cualquier_valor(self, client):
        """`step` no es sólo el salto de las flechas: el navegador rechazaba
        cualquier valor fuera del escalón, y «radio 25» salía inválido."""
        html = client.get("/buscar").text
        import re

        pasos = re.findall(r'<input type="number"[^>]*step="([^"]+)"', html)
        assert pasos and all(p == "any" for p in pasos)

    def test_el_municipio_se_situa_sin_tener_la_base_cargada(
        self, client, fuentes_simuladas, monkeypatch
    ):
        """Escribir «Noja» no hacía nada: el municipio se buscaba sólo en la
        base, y con la base vacía —que es cuando se usa esto— nunca estaba."""
        from app.sources.osm import NominatimSource

        monkeypatch.setattr(
            NominatimSource, "geocode",
            lambda self, query: {"lat": 43.4869, "lon": -3.5290,
                                 "display_name": "Noja, Cantabria"},
        )
        respuesta = client.post("/buscar/descubrir", data={"q": "Noja"})
        assert respuesta.status_code == 200
        assert "Nominatim" in respuesta.text
        assert "43.4869" in respuesta.text

    def test_si_no_se_situa_el_municipio_lo_dice(self, client, fuentes_simuladas):
        respuesta = client.post("/buscar/descubrir", data={"q": "Sitio inexistente"})
        assert "No se ha podido situar" in respuesta.text
        # Y aun así se han consultado las fuentes que sí filtran por provincia.
        assert "Subastas del BOE" in respuesta.text

    def test_dice_que_los_portales_necesitan_un_punto(self, client, fuentes_simuladas):
        """Sin coordenadas la tabla se quedaba a medias sin explicar por qué."""
        respuesta = client.post("/buscar/descubrir", data={"province": "Cantabria"})
        assert "Busca por coordenadas" in respuesta.text

    def test_lista_las_candidatas_con_su_casilla(self, client, fuentes_simuladas):
        respuesta = client.post("/buscar/descubrir", data={"province": "Cantabria"})
        assert respuesta.status_code == 200
        assert 'name="candidato"' in respuesta.text
        assert "Subastas del BOE" in respuesta.text
        assert "Incorporar las marcadas" in respuesta.text

    def test_sin_marcar_ninguna_lo_dice(self, client):
        respuesta = client.post("/buscar/importar", data={"province": "Cantabria"})
        assert respuesta.status_code == 200
        assert "No has marcado ninguna candidata" in respuesta.text

    def test_importar_desde_el_formulario_puebla_la_base(self, client, fuentes_simuladas):
        from app.discovery import discover, serialize
        from app.db import SessionLocal

        with SessionLocal() as db:
            candidatas = discover(db, province="Cantabria")["candidates"]
        pendientes = [c for c in candidatas if not c["already_saved"]]
        if not pendientes:                      # otro test pudo importarlas ya
            pendientes = candidatas

        respuesta = client.post(
            "/buscar/importar",
            data={"province": "Cantabria",
                  "candidato": [serialize(c) for c in pendientes]},
        )
        assert respuesta.status_code == 200
        assert "actualizadas" in respuesta.text

        # Y ahora el buscador ya tiene algo que enseñar.
        html = client.get("/buscar", params={"min_discount_pct": 0}).text
        assert "No hay ningún anuncio cargado" not in html


class TestConsolaDeRapidApi:
    """La página que rompe el ciclo «cambio código, despliego, miro el panel».

    Estos revendedores contestan «Invalid request parameters» sin decir cuál,
    y hacen falta ocho o diez pruebas para dar con la combinación. A un
    despliegue por prueba eso no se acaba nunca.
    """

    def _pagina(self, client):
        return client.get("/fuentes/rapidapi")

    def test_la_pagina_carga_sin_tocar_la_red(self, client):
        """Sólo pinta el formulario: las peticiones las lanza el usuario."""
        respuesta = self._pagina(client)
        assert respuesta.status_code == 200
        assert "Consola de RapidAPI" in respuesta.text

    def test_enseña_la_huella_de_la_clave_de_cada_fuente(self, client, monkeypatch):
        """Es lo que permite ver que el servidor usa una clave distinta de la
        que uno prueba a mano, que es lo que estaba pasando."""
        from app.config import get_settings

        monkeypatch.setenv("RAPIDAPI_KEY", "clave-de-prueba-1234")
        get_settings.cache_clear()
        texto = self._pagina(client).text
        get_settings.cache_clear()
        assert "clave-…1234" in texto
        # Y nunca la clave entera.
        assert "clave-de-prueba-1234" not in texto

    def test_enseña_el_host_y_las_rutas_que_prueba(self, client):
        texto = self._pagina(client).text
        assert "idealista17.p.rapidapi.com" in texto
        assert "/property-search-by-coordinates" in texto

    def test_trae_atajos_de_llamadas_que_ya_responden(self, client):
        """Empezar de algo que funciona y cambiar una cosa cada vez es lo que
        permite aislar el parámetro que falla."""
        texto = self._pagina(client).text
        assert "idealista17 · coordenadas" in texto
        # Los de Fotocasa van numerados: busca por zona, así que hay que sacar
        # su identificador antes de poder buscar.
        assert "fotocasa · 1. sacar la zona" in texto
        assert "fotocasa · 2. buscar (como lo manda la app)" in texto

    def test_el_panel_de_fuentes_enlaza_la_consola(self, client):
        assert '/fuentes/rapidapi' in client.get("/fuentes").text


class TestAtajosQueReproducenAlConector:
    """Un atajo con menos parámetros que la app falla por otra razón.

    Pasó: el atajo de Fotocasa omitía `combinedLocations` y `sortType`, así que
    el proveedor se quejaba de eso y no de lo que de verdad rechaza cuando la
    app le habla. Un atajo que no reproduce la llamada real no depura nada.
    """

    def _presets(self, client):
        import re, json

        texto = client.get("/fuentes/rapidapi").text
        crudo = re.search(r"const PRESETS = (\{.*?\});", texto, re.DOTALL)
        return json.loads(crudo.group(1))

    def test_el_atajo_de_fotocasa_manda_lo_mismo_que_el_conector(self, client):
        from app.sources.rapidapi import RapidApiFotocasaSource

        preset = self._presets(client)["fotocasa · 2. buscar (como lo manda la app)"]
        del_conector = set(RapidApiFotocasaSource().build_params(43.46, -3.81, 10.0))
        del_conector.add("combinedLocations")   # lo añade el paso previo
        assert del_conector <= set(preset["params"])

    def test_el_primer_paso_pide_la_zona(self, client):
        from app.sources.rapidapi import RapidApiFotocasaSource

        preset = self._presets(client)["fotocasa · 1. sacar la zona"]
        assert preset["path"] == RapidApiFotocasaSource.suggestions_path
        assert "query" in preset["params"]


class TestVersionDesplegada:
    """«¿Está corriendo lo último?» no debería ser una adivinanza.

    El panel de Fly llegó a enseñar el commit de una rama vieja mientras el
    workflow había desplegado main correctamente, y no había forma de saber
    cuál de los dos decía la verdad. Ahora se le pregunta a la aplicación, que
    lleva dentro el commit del que salió su imagen.
    """

    def test_health_dice_la_version(self, client):
        datos = client.get("/health").json()
        assert datos["status"] == "ok"
        assert "version" in datos

    def test_sin_commit_inyectado_lo_dice_en_vez_de_inventarlo(self, client, monkeypatch):
        """«desconocida» es una respuesta honesta; «main» sería mentira."""
        monkeypatch.delenv("GIT_SHA", raising=False)
        assert client.get("/version").json()["version"] == "desconocida"

    def test_con_commit_inyectado_lo_publica_y_enlaza(self, client, monkeypatch):
        monkeypatch.setenv("GIT_SHA", "2bcadd7e1b9b1b5de96dd1b05a208b27692579c2")
        datos = client.get("/version").json()
        assert datos["version"] == "2bcadd7e1b9b1b5de96dd1b05a208b27692579c2"
        assert datos["commit_url"].endswith(datos["version"])

    def test_sin_version_no_se_enlaza_a_ninguna_parte(self, client, monkeypatch):
        monkeypatch.delenv("GIT_SHA", raising=False)
        assert client.get("/version").json()["commit_url"] == ""
