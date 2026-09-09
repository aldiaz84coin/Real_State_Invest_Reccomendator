"""Tests de la capa de conectores: reintentos y clasificación de estados.

El fallo que motivó estos tests es real: el servicio OVC del Catastro cerró la
conexión sin responder y la app lo reportó como fuente caída. Aquí se reproduce
ese corte con un servidor local que se comporta igual.
"""
import json
import socket
import threading

import httpx
import pytest

from app.sources.base import BaseSource
from app.sources.catastro import CatastroSource


class FlakyServer:
    """Servidor que corta la conexión las primeras `fail_times` peticiones.

    Reproduce exactamente el `RemoteProtocolError: Server disconnected without
    sending a response` que devolvía el Catastro: acepta la conexión y la
    cierra sin escribir nada.
    """

    def __init__(self, fail_times: int, body: str = "OK"):
        self.fail_times = fail_times
        self.body = body
        self.requests = 0
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            self.requests += 1
            try:
                connection.recv(65535)
                if self.requests <= self.fail_times:
                    # Cierre abrupto, sin responder nada.
                    connection.close()
                    continue
                payload = self.body.encode()
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + payload
                )
            finally:
                try:
                    connection.close()
                except OSError:
                    pass


class DummySource(BaseSource):
    key = "dummy"
    name = "Dummy"
    kind = "geo"

    def check(self):
        return self._status("ok")


@pytest.fixture
def source(monkeypatch):
    src = DummySource()
    # Sin espera real: los tests no deben tardar segundos por el backoff.
    monkeypatch.setattr("app.sources.base.time.sleep", lambda _s: None)
    return src


class TestReintentos:
    def test_se_recupera_de_un_corte_de_conexion(self, source):
        with FlakyServer(fail_times=1, body="recuperado") as server:
            response = source.request("GET", server.url)
        assert response.status_code == 200
        assert response.text == "recuperado"
        assert server.requests == 2  # falló una vez, acertó a la segunda

    def test_se_recupera_de_dos_cortes(self, source):
        with FlakyServer(fail_times=2) as server:
            response = source.request("GET", server.url)
        assert response.status_code == 200
        assert server.requests == 3

    def test_se_rinde_tras_agotar_los_intentos(self, source):
        with FlakyServer(fail_times=99) as server:
            with pytest.raises(httpx.HTTPError):
                source.request("GET", server.url)
            assert server.requests == 3  # no insiste indefinidamente

    def test_numero_de_intentos_configurable(self, source):
        with FlakyServer(fail_times=99) as server:
            with pytest.raises(httpx.HTTPError):
                source.request("GET", server.url, attempts=5)
            assert server.requests == 5

    def test_no_reintenta_errores_permanentes(self, source, monkeypatch):
        """Un 404 no mejora reintentando: solo añadiría latencia."""
        calls = {"n": 0}

        def fake_request(self, method, url, **kwargs):
            calls["n"] += 1
            return httpx.Response(404, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake_request)
        response = source.request("GET", "http://ejemplo.invalido/")
        assert response.status_code == 404
        assert calls["n"] == 1

    def test_reintenta_un_503(self, source, monkeypatch):
        calls = {"n": 0}

        def fake_request(self, method, url, **kwargs):
            calls["n"] += 1
            code = 503 if calls["n"] == 1 else 200
            return httpx.Response(code, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake_request)
        response = source.request("GET", "http://ejemplo.invalido/")
        assert response.status_code == 200
        assert calls["n"] == 2

    def test_no_reintenta_un_bloqueo_del_proxy(self, source, monkeypatch):
        """Una denegación de política de red no se arregla insistiendo.

        Sale como SourceBlocked y no como httpx.ProxyError: al ser SourceError
        lo cubre cualquier código que ya atrapaba fallos de fuente. Dejarlo
        escapar como excepción de httpx provocaba errores 500 en los
        endpoints, y era un fallo que reaparecía en cada conector nuevo.
        """
        from app.sources.base import SourceBlocked, SourceError

        calls = {"n": 0}

        def fake_request(self, method, url, **kwargs):
            calls["n"] += 1
            raise httpx.ProxyError("403 Forbidden")

        monkeypatch.setattr(httpx.Client, "request", fake_request)
        with pytest.raises(SourceBlocked) as error:
            source.request("GET", "http://ejemplo.invalido/")
        assert calls["n"] == 1
        assert isinstance(error.value, SourceError)   # el contrato que importa


class TestClasificacionDeEstado:
    def test_un_corte_persistente_se_explica_como_pasajero(self, source):
        with FlakyServer(fail_times=99) as server:
            status = source._timed_probe(server.url)
        assert status.access == "error"
        assert "pasajera" in status.detail
        assert not status.ok

    def test_una_fuente_sana_sale_ok(self, source):
        with FlakyServer(fail_times=0) as server:
            status = source._timed_probe(server.url)
        assert status.access == "ok"
        assert status.ok
        assert status.latency_ms is not None

    def test_el_401_se_reporta_como_falta_de_credenciales(self, source, monkeypatch):
        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(401, request=httpx.Request(method, url)),
        )
        status = source._timed_probe("http://ejemplo.invalido/")
        assert status.access == "needs_credentials"


class TestCatastro:
    def test_el_gml_se_convierte_a_geojson(self):
        """El Catastro sirve EPSG:4326 en orden lat,lon y GeoJSON exige lon,lat."""
        gml = """<?xml version="1.0"?>
        <FeatureCollection xmlns:gml="http://www.opengis.net/gml/3.2">
          <member><CadastralParcel>
            <areaValue>1234.56</areaValue>
            <gml:posList>36.7213 -4.4214 36.7215 -4.4214 36.7215 -4.4210 36.7213 -4.4210</gml:posList>
          </CadastralParcel></member>
        </FeatureCollection>"""
        feature = CatastroSource.parse_parcel_gml(gml, "REF123")
        ring = feature["geometry"]["coordinates"][0]
        assert feature["properties"]["official_area_m2"] == pytest.approx(1234.56)
        assert feature["properties"]["cadastral_ref"] == "REF123"
        # Primer punto: longitud negativa (oeste), latitud positiva.
        assert ring[0] == [-4.4214, 36.7213]
        # El anillo debe quedar cerrado.
        assert ring[0] == ring[-1]

    def test_gml_sin_geometria_devuelve_none(self):
        assert CatastroSource.parse_parcel_gml("<FeatureCollection/>") is None

    def test_gml_ilegible_avisa(self):
        from app.sources.base import SourceError

        with pytest.raises(SourceError, match="ilegible"):
            CatastroSource.parse_parcel_gml("esto no es xml <<<")


class TestRapidApiNormalizacion:
    """El esquema de estos revendedores no es verificable sin clave y cambia sin
    aviso, así que el adaptador debe tolerar envoltorios y nombres distintos.
    Estos tests fijan esa tolerancia con formas de respuesta realistas."""

    @staticmethod
    def _source():
        from app.sources.rapidapi import RapidApiIdealistaSource

        return RapidApiIdealistaSource()

    def test_encuentra_anuncios_en_envoltorio_estilo_idealista(self):
        from app.sources.rapidapi import extract_listings

        payload = {"total": 2, "elementList": [{"price": 1}, {"price": 2}]}
        assert len(extract_listings(payload)) == 2

    def test_encuentra_anuncios_en_envoltorios_alternativos(self):
        from app.sources.rapidapi import extract_listings

        for wrapper in ("data", "results", "items", "listings", "properties", "hits"):
            payload = {wrapper: [{"priceAmount": 100}]}
            assert len(extract_listings(payload)) == 1, wrapper

    def test_encuentra_anuncios_anidados_en_profundidad(self):
        from app.sources.rapidapi import extract_listings

        payload = {"response": {"body": {"search": {"rows": [{"price": 5}]}}}}
        assert len(extract_listings(payload)) == 1

    def test_lista_en_la_raiz(self):
        from app.sources.rapidapi import extract_listings

        assert len(extract_listings([{"price": 1}, {"price": 2}])) == 2

    def test_ignora_listas_que_no_son_anuncios(self):
        from app.sources.rapidapi import extract_listings

        # Ni los filtros ni las facetas llevan precio: no deben confundirse.
        payload = {"filters": [{"name": "zona"}], "facets": [{"count": 3}]}
        assert extract_listings(payload) == []

    def test_respuesta_vacia(self):
        from app.sources.rapidapi import extract_listings

        assert extract_listings({}) == []
        assert extract_listings([]) == []
        assert extract_listings(None) == []

    def test_normaliza_esquema_estilo_idealista(self):
        item = {
            "propertyCode": "98765",
            "price": 52000,
            "size": 1100,
            "latitude": 36.5101,
            "longitude": -4.8825,
            "address": "Camino de los Pinos",
            "municipality": "Marbella",
            "province": "Málaga",
            "url": "https://ejemplo/98765",
            "detailedType": {"subTypology": "terrain"},
        }
        result = self._source().normalize(item)
        assert result["external_id"] == "98765"
        assert result["price_eur"] == 52000
        assert result["area_m2"] == 1100
        assert result["price_eur_m2"] == pytest.approx(47.27, abs=0.01)
        assert result["municipality_name"] == "Marbella"
        assert result["land_type"] == "terrain"
        assert result["source"] == "rapidapi_idealista"

    def test_normaliza_nombres_de_campo_alternativos(self):
        """Otro proveedor, otros nombres: debe salir el mismo resultado."""
        item = {
            "id": 42,
            "priceAmount": 80000,
            "surfaceArea": 2000,
            "coordinates": {"latitude": 40.4, "longitude": -3.7},
            "city": "Madrid",
            "link": "https://ejemplo/42",
        }
        result = self._source().normalize(item)
        assert result["external_id"] == "42"
        assert result["price_eur"] == 80000
        assert result["area_m2"] == 2000
        assert result["lat"] == pytest.approx(40.4)
        assert result["municipality_name"] == "Madrid"

    def test_acepta_numeros_con_formato_espanol(self):
        item = {
            "id": "x", "price": "125.000,50 €", "size": "1.250",
            "lat": 36.7, "lng": -4.4,
        }
        result = self._source().normalize(item)
        assert result["price_eur"] == pytest.approx(125000.50)
        assert result["area_m2"] == pytest.approx(1250)

    @pytest.mark.parametrize("falta", ["price", "size", "propertyCode"])
    def test_descarta_anuncios_sin_datos_imprescindibles(self, falta):
        """Sin precio, superficie o identificador el anuncio no sirve."""
        item = {
            "propertyCode": "1", "price": 1000, "size": 500,
            "latitude": 36.7, "longitude": -4.4,
        }
        del item[falta]
        assert self._source().normalize(item) is None

    def test_las_coordenadas_que_faltan_no_descartan_el_anuncio(self):
        """Cambio deliberado: la búsqueda de algunos proveedores no las trae, y
        exigirlas vaciaba el resultado entero sin explicación."""
        item = {"propertyCode": "1", "price": 1000, "size": 500}
        result = self._source().normalize(item)
        assert result is not None
        assert result["coords_precision"] == "missing"

    def test_descarta_superficie_cero(self):
        item = {"propertyCode": "1", "price": 1000, "size": 0,
                "latitude": 36.7, "longitude": -4.4}
        assert self._source().normalize(item) is None

    def test_sin_clave_no_esta_configurada(self):
        assert not self._source().configured

    def test_sin_clave_el_estado_es_falta_credenciales(self):
        status = self._source().check()
        assert status.access == "needs_credentials"
        assert "no oficial" in status.detail.lower()

    def test_buscar_sin_clave_avisa_en_vez_de_reventar(self):
        from app.sources.base import SourceError

        with pytest.raises(SourceError, match="RAPIDAPI_KEY"):
            self._source().search_lands(36.7, -4.4)

    def test_las_cabeceras_llevan_host_y_clave(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("RAPIDAPI_KEY", "clave-de-prueba")
        get_settings.cache_clear()
        try:
            source = self._source()
            assert source.configured
            headers = source.headers()
            assert headers["x-rapidapi-host"] == "idealista-api1.p.rapidapi.com"
            assert headers["x-rapidapi-key"] == "clave-de-prueba"
        finally:
            get_settings.cache_clear()

    def test_fotocasa_tiene_su_propio_host(self):
        from app.sources.rapidapi import RapidApiFotocasaSource

        assert RapidApiFotocasaSource().host == "fotocasa3.p.rapidapi.com"
        assert RapidApiFotocasaSource().portal == "Fotocasa"


class TestRegistroDeFuentes:
    def test_los_respaldos_aparecen_en_el_registro(self):
        from app.sources.registry import build_sources

        keys = {s.key for s in build_sources()}
        assert "rapidapi_idealista" in keys
        assert "rapidapi_fotocasa" in keys

    def test_la_via_oficial_va_antes_que_el_respaldo(self):
        """El orden importa: el respaldo no debe desplazar a la API oficial."""
        from app.sources.registry import build_sources

        keys = [s.key for s in build_sources()]
        assert keys.index("idealista") < keys.index("rapidapi_idealista")


class TestParseoNumerico:
    """El separador suelto es ambiguo y era la fuente de un error real:
    "1.250" se leía como 1,25 en vez de 1250."""

    @pytest.mark.parametrize(
        "entrada,esperado",
        [
            ("125.000,50", 125000.50),   # español completo
            ("1,234.56", 1234.56),       # inglés completo
            ("1.250", 1250.0),           # millares españoles
            ("1,250", 1250.0),           # millares ingleses
            ("1.234.567", 1234567.0),    # varios separadores
            ("1.5", 1.5),                # decimal de un dígito
            ("1,5", 1.5),
            ("52000", 52000.0),
            ("52000 €", 52000.0),
            (1100, 1100.0),
            (1100.5, 1100.5),
            ("", None),
            ("no es un número", None),
            (None, None),
            (True, None),                # un booleano no es una cantidad
        ],
    )
    def test_conversion(self, entrada, esperado):
        from app.sources.rapidapi import _as_float

        resultado = _as_float(entrada)
        if esperado is None:
            assert resultado is None
        else:
            assert resultado == pytest.approx(esperado)


class TestIdealista17:
    """Contrastado con el ejemplo de respuesta de su propia documentación."""

    EJEMPLO_DOC = {
        "total": 12847,
        "totalPages": 322,
        "actualPage": 1,
        "itemsPerPage": 40,
        "elementList": [
            {
                "propertyCode": "112345678",
                "price": 385000,
                "size": 92,
                "rooms": 3,
                "bathrooms": 2,
                "municipality": "Madrid",
                "district": "Chamberi",
                "propertyType": "flat",
                "operation": "sale",
                "url": "https://www.idealista.com/inmueble/112345678/",
            }
        ],
    }

    @staticmethod
    def _source():
        from app.sources.rapidapi import RapidApiIdealista17Source

        return RapidApiIdealista17Source()

    def test_usa_las_rutas_reales_de_su_documentacion(self):
        """La de coordenadas va primera: es la que encaja con cómo busca esta
        aplicación, un punto y un radio."""
        paths = self._source().search_paths
        assert paths[0] == "/property-search-by-coordinates"
        assert "/property-search" in paths

    def test_localiza_los_anuncios_del_ejemplo_documentado(self):
        from app.sources.rapidapi import extract_listings

        assert len(extract_listings(self.EJEMPLO_DOC)) == 1

    def test_la_busqueda_no_devuelve_coordenadas_y_aun_asi_se_conserva(self):
        """Es el caso real: su respuesta de búsqueda no trae lat/lon.

        Exigirlas descartaría todos los anuncios en silencio, que es
        justamente el fallo que este comportamiento evita.
        """
        from app.sources.rapidapi import extract_listings

        item = extract_listings(self.EJEMPLO_DOC)[0]
        result = self._source().normalize(item)
        assert result is not None
        assert result["lat"] is None and result["lon"] is None
        assert result["coords_precision"] == "missing"
        assert result["external_id"] == "112345678"
        assert result["price_eur"] == 385000
        assert result["area_m2"] == 92
        assert result["municipality_name"] == "Madrid"
        assert result["price_eur_m2"] == pytest.approx(4184.78, abs=0.01)

    def test_marca_como_exactas_las_coordenadas_cuando_vienen(self):
        item = dict(self.EJEMPLO_DOC["elementList"][0], latitude=40.4, longitude=-3.7)
        result = self._source().normalize(item)
        assert result["coords_precision"] == "exact"
        assert result["lat"] == pytest.approx(40.4)

    def test_sigue_descartando_lo_que_no_sirve(self):
        source = self._source()
        assert source.normalize({"propertyCode": "1", "size": 100}) is None      # sin precio
        assert source.normalize({"propertyCode": "1", "price": 100}) is None     # sin superficie
        assert source.normalize({"price": 100, "size": 10}) is None              # sin id

    def test_explica_el_motivo_del_descarte(self):
        source = self._source()
        assert source.discard_reason({"propertyCode": "1", "size": 10}) == "sin precio"
        assert source.discard_reason({"propertyCode": "1", "price": 10}) == "sin superficie"
        assert source.discard_reason({"price": 10, "size": 10}) == "sin identificador"

    def test_los_parametros_de_busqueda_son_de_terrenos_en_venta(self):
        """En snake_case, que es como los nombra este revendedor.

        En camelCase -como la API oficial de Idealista- contestaba HTTP 400,
        «Invalid or missing parameters», y se leía como si la ruta estuviera
        mal cuando lo que estaba mal eran los nombres.
        """
        params = self._source().build_params(36.72, -4.42, 15.0, page=2, max_price=90000)
        assert params["search_type"] == "for_sale"
        assert params["property_type"] == "lands"
        assert params["country"] == "es"
        # El radio va en kilómetros, no en metros.
        assert params["radius_km"] == 15
        assert params["page"] == 2
        assert params["max_price"] == 90000
        # Obligatorios en la práctica pese a parecer opcionales.
        assert params["sort_order"] == "default"
        assert params["result_count"] == 30
        # Los de la API oficial no valen aquí y no deben colarse.
        for inventado in ("propertyType", "operation", "locale", "maxItems", "radius"):
            assert inventado not in params

    def test_clave_propia_por_fuente(self, monkeypatch):
        """RapidAPI da una clave por aplicación y suele haber una por API."""
        from app.config import get_settings

        monkeypatch.setenv("RAPIDAPI_KEYS", "rapidapi_idealista17=clave-17")
        monkeypatch.setenv("RAPIDAPI_KEY", "clave-global")
        get_settings.cache_clear()
        try:
            from app.sources.rapidapi import RapidApiFotocasaSource

            assert self._source().api_key == "clave-17"
            # Sin clave propia, cae a la global.
            assert RapidApiFotocasaSource().api_key == "clave-global"
        finally:
            get_settings.cache_clear()

    def test_el_cuerpo_de_error_del_proveedor_se_detecta(self, monkeypatch):
        """Devuelve 200 con {"error": ...}: no debe pasar por resultado vacío."""
        from app.sources.base import SourceError
        from app.config import get_settings

        monkeypatch.setenv("RAPIDAPI_KEY", "x")
        get_settings.cache_clear()
        try:
            source = self._source()
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200,
                    json={"error": "upstream_unavailable",
                          "message": "Upstream source temporarily unavailable"},
                    request=httpx.Request(method, url),
                ),
            )
            with pytest.raises(SourceError, match="upstream_unavailable"):
                source.fetch_page({})
        finally:
            get_settings.cache_clear()

    def test_prueba_la_siguiente_ruta_ante_un_404(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("RAPIDAPI_KEY", "x")
        get_settings.cache_clear()
        try:
            source = self._source()
            vistos: list[str] = []

            def fake(self, method, url, **kw):
                vistos.append(url)
                # La primera ruta no existe; debe seguir con la siguiente.
                ruta = str(url).split("?")[0]
                code = 404 if ruta.endswith("by-coordinates") else 200
                return httpx.Response(
                    code, json=TestIdealista17.EJEMPLO_DOC if code == 200 else {},
                    request=httpx.Request(method, url),
                )

            monkeypatch.setattr(httpx.Client, "request", fake)
            path, payload = source.fetch_page({})
            assert path == "/property-search"
            assert len(vistos) == 2  # probó la primera y pasó a la segunda
        finally:
            get_settings.cache_clear()

    def test_una_clave_rechazada_no_prueba_mas_rutas(self, monkeypatch):
        """Un 401 es problema de suscripción, no de ruta: insistir sólo gasta cuota."""
        from app.sources.base import SourceError
        from app.config import get_settings

        monkeypatch.setenv("RAPIDAPI_KEY", "x")
        get_settings.cache_clear()
        try:
            source = self._source()
            llamadas = {"n": 0}

            def fake(self, method, url, **kw):
                llamadas["n"] += 1
                return httpx.Response(401, request=httpx.Request(method, url))

            monkeypatch.setattr(httpx.Client, "request", fake)
            with pytest.raises(SourceError, match="HTTP 401"):
                source.fetch_page({})
            assert llamadas["n"] == 1
        finally:
            get_settings.cache_clear()


class TestDiagnosticoDelPanel:
    """El panel mostró un 404 en Fotocasa que la búsqueda real habría superado,
    porque el health check probaba sólo la primera ruta candidata. Estos tests
    fijan que el sondeo use el mismo mecanismo que la búsqueda."""

    @staticmethod
    def _source(monkeypatch, cls=None):
        from app.config import get_settings
        from app.sources.rapidapi import RapidApiIdealista17Source, clear_check_cache

        monkeypatch.setenv("RAPIDAPI_KEY", "clave")
        get_settings.cache_clear()
        # La caché de comprobaciones es global: sin limpiarla, un test vería
        # el resultado del anterior.
        clear_check_cache()
        return (cls or RapidApiIdealista17Source)()

    def test_el_sondeo_prueba_las_rutas_alternativas(self, monkeypatch):
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            visitadas: list[str] = []
            ruta_buena = source.search_paths[1]

            def fake(self, method, url, **kw):
                visitadas.append(url)
                if url.endswith(ruta_buena):
                    return httpx.Response(
                        200, json={"elementList": [{"price": 1, "size": 1}]},
                        request=httpx.Request(method, url),
                    )
                return httpx.Response(404, request=httpx.Request(method, url))

            monkeypatch.setattr(httpx.Client, "request", fake)
            status = source.check()
            assert status.access == "ok", status.detail
            assert len(visitadas) == 2
            assert ruta_buena in status.detail
            assert "Anuncios localizados en la prueba: 1" in status.detail
        finally:
            get_settings.cache_clear()

    def test_el_401_se_explica_sin_dar_por_hecho_la_causa(self, monkeypatch):
        """En RapidAPI hay proveedores que contestan 401 a «no suscrito» y
        otros a «clave inválida», así que el código por sí solo no decide:
        se explican las dos salidas y manda el mensaje del proveedor."""
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    401, request=httpx.Request(method, url)
                ),
            )
            status = source.check()
            assert status.access == "needs_credentials"
            assert "Puede ser la clave o puede ser la suscripción" in status.detail
            assert "suscribe ESA aplicación" in status.detail
            assert "Subscribe to Test" in status.detail
        finally:
            get_settings.cache_clear()

    def test_el_401_no_gasta_cuota_probando_rutas(self, monkeypatch):
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            llamadas = {"n": 0}

            def fake(self, method, url, **kw):
                llamadas["n"] += 1
                return httpx.Response(401, request=httpx.Request(method, url))

            monkeypatch.setattr(httpx.Client, "request", fake)
            source.check()
            assert llamadas["n"] == 1
        finally:
            get_settings.cache_clear()

    def test_avisa_cuando_responde_pero_no_localiza_anuncios(self, monkeypatch):
        """Un 200 vacío no es lo mismo que funcionar: puede ser mapeo roto."""
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200, json={"algo": "que no son anuncios"},
                    request=httpx.Request(method, url),
                ),
            )
            status = source.check()
            assert status.access == "ok"
            assert "Anuncios localizados en la prueba: 0" in status.detail
            assert "probe" in status.detail
        finally:
            get_settings.cache_clear()

    def test_sin_clave_no_hace_ninguna_peticion(self, monkeypatch):
        from app.config import get_settings
        from app.sources.rapidapi import RapidApiFotocasaSource, clear_check_cache

        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
        monkeypatch.delenv("RAPIDAPI_KEYS", raising=False)
        get_settings.cache_clear()
        clear_check_cache()
        try:
            def fake(self, method, url, **kw):
                raise AssertionError("no debería pedir nada sin clave")

            monkeypatch.setattr(httpx.Client, "request", fake)
            status = RapidApiFotocasaSource().check()
            assert status.access == "needs_credentials"
        finally:
            get_settings.cache_clear()


class TestCuotaDeRapidApi:
    """Cada comprobación cuesta una petición del plan y el panel sondea todas
    las fuentes en cada carga. Sin estos dos frenos, unas pocas visitas al día
    agotarían un plan gratuito de 500 al mes sólo comprobando."""

    @staticmethod
    def _fotocasa(monkeypatch):
        from app.config import get_settings
        from app.sources.rapidapi import RapidApiFotocasaSource, clear_check_cache

        monkeypatch.setenv("RAPIDAPI_KEY", "clave")
        get_settings.cache_clear()
        clear_check_cache()
        return RapidApiFotocasaSource()

    def test_fotocasa_usa_su_endpoint_de_salud_y_no_una_busqueda(self, monkeypatch):
        from app.config import get_settings

        source = self._fotocasa(monkeypatch)
        try:
            urls: list[str] = []

            def fake(self, method, url, **kw):
                urls.append(url)
                return httpx.Response(200, json={"status": "ok"},
                                      request=httpx.Request(method, url))

            monkeypatch.setattr(httpx.Client, "request", fake)
            status = source.check()
            assert status.access == "ok"
            assert len(urls) == 1
            assert urls[0].endswith("/health")   # no gastó una búsqueda
        finally:
            get_settings.cache_clear()

    def test_la_comprobacion_se_cachea(self, monkeypatch):
        from app.config import get_settings

        source = self._fotocasa(monkeypatch)
        try:
            llamadas = {"n": 0}

            def fake(self, method, url, **kw):
                llamadas["n"] += 1
                return httpx.Response(200, json={}, request=httpx.Request(method, url))

            monkeypatch.setattr(httpx.Client, "request", fake)
            source.check()
            source.check()
            source.check()
            # Tres cargas del panel, una sola petición a la API.
            assert llamadas["n"] == 1
        finally:
            get_settings.cache_clear()

    def test_la_cache_caduca(self, monkeypatch):
        from app.config import get_settings
        import app.sources.rapidapi as mod

        source = self._fotocasa(monkeypatch)
        try:
            llamadas = {"n": 0}

            def fake(self, method, url, **kw):
                llamadas["n"] += 1
                return httpx.Response(200, json={}, request=httpx.Request(method, url))

            monkeypatch.setattr(httpx.Client, "request", fake)
            source.check()
            # Se envejece la entrada más allá del TTL. El reloj real se captura
            # antes de parchear, o la sustitución se llamaría a sí misma.
            ahora = mod.time.time()
            monkeypatch.setattr(
                mod.time, "time", lambda: ahora + mod.CHECK_CACHE_SECONDS + 1
            )
            source.check()
            assert llamadas["n"] == 2
        finally:
            get_settings.cache_clear()

    def test_fotocasa_busca_en_la_ruta_real_de_su_documentacion(self):
        from app.sources.rapidapi import RapidApiFotocasaSource

        # /searchads, no /search: las cuatro rutas anteriores daban 404.
        assert RapidApiFotocasaSource().search_paths[0] == "/searchads"


class TestFotocasaApiReal:
    """Contrastado con la documentación del proveedor. Su API busca por zona,
    no por radio, así que la búsqueda son dos pasos."""

    @staticmethod
    def _source(monkeypatch):
        from app.config import get_settings
        from app.sources.rapidapi import RapidApiFotocasaSource, clear_check_cache

        monkeypatch.setenv("RAPIDAPI_KEY", "clave")
        get_settings.cache_clear()
        clear_check_cache()
        return RapidApiFotocasaSource()

    def test_los_parametros_son_los_documentados(self, monkeypatch):
        source = self._source(monkeypatch)
        params = source.build_params(
            40.4096, -3.68624, 15, page=2,
            combined_locations="724,14,28,173,0,28079,0,0,0",
            min_size_m2=300, max_size_m2=5000, max_price=90000,
        )
        # Nombres exactos de su documentación, no los genéricos de antes.
        assert params["transactionType"] == "BUY"
        assert params["propertyType"] == "LAND"       # terrenos
        assert params["pageNumber"] == 2
        assert params["combinedLocations"] == "724,14,28,173,0,28079,0,0,0"
        assert params["minSurface"] == 300
        assert params["maxSurface"] == 5000
        assert params["maxPrice"] == 90000
        assert "publicationDate" in params            # figura como requerido
        # Los que enviaba antes ya no existen en su API.
        assert "operation" not in params and "distance" not in params

    def test_busca_en_searchads(self, monkeypatch):
        assert self._source(monkeypatch).search_paths == ("/searchads",)

    def test_resuelve_la_zona_por_suggestions(self, monkeypatch):
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            vistos: list[str] = []

            def fake(self, method, url, **kw):
                vistos.append(url)
                return httpx.Response(
                    200,
                    json={"data": [
                        {"combinedLocationIds": "724,14,28,173,0,28079,0,0,0",
                         "coordinates": {"latitude": 40.4, "longitude": -3.7},
                         "text": "Madrid, Madrid"}
                    ]},
                    request=httpx.Request(method, url),
                )

            monkeypatch.setattr(httpx.Client, "request", fake)
            combined = source.resolve_location(40.4, -3.7, query="madrid")
            assert combined == "724,14,28,173,0,28079,0,0,0"
            assert vistos[0].endswith("/suggestions")
        finally:
            get_settings.cache_clear()

    def test_la_zona_resuelta_se_cachea(self, monkeypatch):
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            llamadas = {"n": 0}

            def fake(self, method, url, **kw):
                llamadas["n"] += 1
                return httpx.Response(
                    200,
                    json={"data": [{"combinedLocationIds": "1,2,3,4,5,6,0,0,0",
                                    "coordinates": {"latitude": 40.4, "longitude": -3.7}}]},
                    request=httpx.Request(method, url),
                )

            monkeypatch.setattr(httpx.Client, "request", fake)
            source.resolve_location(40.4, -3.7, query="madrid")
            source.resolve_location(40.4, -3.7, query="madrid")
            assert llamadas["n"] == 1   # no repite la llamada por cada página
        finally:
            get_settings.cache_clear()

    def test_avisa_si_suggestions_no_trae_el_identificador(self, monkeypatch):
        """El error debe llevar la respuesta recibida.

        Remitir al diagnóstico era circular: si la resolución falla, el probe
        de búsqueda falla en el mismo punto y no llega a enseñar nada.
        """
        from app.config import get_settings
        from app.sources.base import SourceError

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200, json={"data": [{"nombre": "Madrid", "zoneId": 28079,
                                         "coordinates": {"latitude": 40.4, "longitude": -3.7}}]},
                    request=httpx.Request(method, url),
                ),
            )
            with pytest.raises(SourceError) as error:
                source.resolve_location(40.4, -3.7, query="madrid")
            mensaje = str(error.value)
            assert "sugerencia" in mensaje
            assert "zoneId" in mensaje       # el cuerpo real viaja en el error
            assert "28079" in mensaje
        finally:
            get_settings.cache_clear()

    def test_reconoce_nombres_alternativos_del_identificador(self, monkeypatch):
        """La documentación dice combinedLocations, pero la respuesta real
        podría llamarlo de otra forma."""
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            for clave in ("combinedLocationIds", "combinedLocations",
                          "combinedLocation", "locationIds"):
                source._locations_cache.clear()
                monkeypatch.setattr(
                    httpx.Client, "request",
                    lambda self, method, url, _k=clave, **kw: httpx.Response(
                        200,
                        json={"data": [{_k: "1,2,3,4,5,6,0,0,0",
                                        "coordinates": {"latitude": 40.4,
                                                        "longitude": -3.7}}]},
                        request=httpx.Request(method, url),
                    ),
                )
                assert source.resolve_location(
                    40.4, -3.7, query="madrid") == "1,2,3,4,5,6,0,0,0", clave
        finally:
            get_settings.cache_clear()

    def test_suggestions_raw_devuelve_el_cuerpo_entero(self, monkeypatch):
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200, json={"data": [{"zoneId": 28079}]},
                    request=httpx.Request(method, url),
                ),
            )
            raw = source.suggestions_raw("madrid")
            assert raw["http_status"] == 200
            assert raw["body"] == {"data": [{"zoneId": 28079}]}
            assert raw["combined_locations_found"] is None
        finally:
            get_settings.cache_clear()

    def test_deduce_el_municipio_del_punto(self, monkeypatch):
        """No se le pide al usuario algo que ya está en las coordenadas."""
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                "app.sources.osm.NominatimSource.reverse",
                lambda self, lat, lon: {"municipality": "Marbella", "province": "Málaga"},
            )
            consultas: list[str] = []

            def fake(self, method, url, **kw):
                consultas.append(kw.get("params", {}).get("query", ""))
                return httpx.Response(
                    200,
                    json={"data": [{"combinedLocationIds": "724,1,29,300,500,29069,0,0,0",
                                    "coordinates": {"latitude": 36.51, "longitude": -4.88}}]},
                    request=httpx.Request(method, url),
                )

            monkeypatch.setattr(httpx.Client, "request", fake)
            params = source.prepare_params(36.51, -4.88, 15, page=1)
            assert params["combinedLocations"] == "724,1,29,300,500,29069,0,0,0"
            assert consultas == ["Marbella"]
        finally:
            get_settings.cache_clear()

    def test_avisa_si_no_se_puede_determinar_el_municipio(self, monkeypatch):
        from app.config import get_settings
        from app.sources.base import SourceError

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                "app.sources.osm.NominatimSource.reverse",
                lambda self, lat, lon: None,
            )
            with pytest.raises(SourceError, match="municipio"):
                source.resolve_location(0.0, 0.0)
        finally:
            get_settings.cache_clear()


class TestBusquedaEnProfundidad:
    def test_encuentra_la_clave_este_donde_este(self):
        from app.sources.rapidapi import _find_key

        assert _find_key({"a": {"b": [{"combinedLocations": "X"}]}}, "combinedlocations") == "X"

    def test_ignora_mayusculas(self):
        from app.sources.rapidapi import _find_key

        assert _find_key({"CombinedLocations": "Y"}, "combinedlocations") == "Y"

    def test_ignora_valores_vacios(self):
        from app.sources.rapidapi import _find_key

        assert _find_key({"combinedLocations": "", "x": {"combinedLocations": "Z"}},
                         "combinedlocations") == "Z"

    def test_devuelve_none_si_no_esta(self):
        from app.sources.rapidapi import _find_key

        assert _find_key({"a": 1}, "combinedlocations") is None


class TestPreparacionDeParametros:
    """El diagnóstico en producción envió /searchads sin combinedLocations y su
    validador respondió 400 «Required». La causa: el probe y el sondeo llamaban
    a build_params, saltándose la resolución de zona que hace prepare_params.
    Estos tests fijan que ambos recorran el mismo camino que la búsqueda real."""

    @staticmethod
    def _fotocasa(monkeypatch):
        from app.config import get_settings
        from app.sources.rapidapi import RapidApiFotocasaSource, clear_check_cache

        monkeypatch.setenv("RAPIDAPI_KEY", "clave")
        get_settings.cache_clear()
        clear_check_cache()
        return RapidApiFotocasaSource()

    def test_prepare_params_incluye_el_identificador_de_zona(self, monkeypatch):
        from app.config import get_settings

        source = self._fotocasa(monkeypatch)
        try:
            monkeypatch.setattr(
                "app.sources.osm.NominatimSource.reverse",
                lambda self, lat, lon: {"municipality": "Málaga", "province": "Málaga"},
            )
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200,
                    json={"data": [{"combinedLocationIds": "724,1,29,319,547,29067,0,0,0",
                                    "coordinates": {"latitude": 36.7217, "longitude": -4.41862}}]},
                    request=httpx.Request(method, url),
                ),
            )
            params = source.prepare_params(36.7213, -4.4214, 15.0, page=1)
            # Es el parámetro que su API declara obligatorio.
            assert params["combinedLocations"] == "724,1,29,319,547,29067,0,0,0"
        finally:
            get_settings.cache_clear()

    def test_build_params_solo_no_lo_incluye(self, monkeypatch):
        """Documenta la diferencia: por eso llamar a build_params daba 400."""
        source = self._fotocasa(monkeypatch)
        assert "combinedLocations" not in source.build_params(36.72, -4.42, 15.0, page=1)

    def test_el_sondeo_pasa_por_prepare_params(self, monkeypatch):
        from app.config import get_settings
        from app.sources.rapidapi import RapidApiIdealista17Source, clear_check_cache

        monkeypatch.setenv("RAPIDAPI_KEY", "clave")
        get_settings.cache_clear()
        clear_check_cache()
        try:
            source = RapidApiIdealista17Source()
            llamadas = {"prepare": 0}
            original = source.prepare_params

            def espia(*args, **kwargs):
                llamadas["prepare"] += 1
                return original(*args, **kwargs)

            monkeypatch.setattr(source, "prepare_params", espia)
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200, json={"elementList": [{"price": 1, "size": 1}]},
                    request=httpx.Request(method, url),
                ),
            )
            source.check()
            assert llamadas["prepare"] == 1
        finally:
            get_settings.cache_clear()

    def test_el_probe_pasa_por_prepare_params(self, monkeypatch):
        """El endpoint de diagnóstico debe recorrer lo mismo que la búsqueda,
        o comprobaría algo que en realidad nunca ocurre."""
        import inspect
        from app import main

        fuente = inspect.getsource(main.api_probe_rapidapi)
        assert "prepare_params" in fuente
        assert "provider.build_params" not in fuente


# Respuesta literal de /suggestions?query=Málaga del proveedor, capturada en
# producción. Es la referencia de todos los tests de selección de zona.
RESPUESTA_SUGGESTIONS_MALAGA = json.loads(r"""{"success":true,"data":[{"localizationLevel5":"","combinedLocationIds":"724,1,29,0,0,0,0,0,0","coordinates":{"longitude":-4.41491,"latitude":36.72},"text":"Málaga","baseText":"Málaga"},{"localizationLevel5":"Málaga","combinedLocationIds":"724,1,29,319,547,29067,0,0,0","coordinates":{"longitude":-4.41862,"latitude":36.7217},"text":"Málaga, Málaga","baseText":"Málaga"},{"localizationLevel5":"Vélez-Málaga","combinedLocationIds":"724,1,29,323,561,29094,0,0,0","coordinates":{"longitude":-4.10077,"latitude":36.784},"text":"Vélez-Málaga, Málaga","baseText":"Vélez-Málaga"},{"localizationLevel5":"Vélez-Málaga","combinedLocationIds":"724,1,29,323,561,29094,0,3553,1029","coordinates":{"longitude":-4.10236,"latitude":36.74332},"text":"Viña Málaga, Vélez-Málaga","baseText":"Viña Málaga"},{"localizationLevel5":"","combinedLocationIds":"724,1,29,319,0,0,0,0,0","coordinates":{"longitude":-4.427950797706377,"latitude":36.722615963289364},"text":"Málaga capital y entorno, Málaga","baseText":"Málaga capital y entorno"},{"localizationLevel5":"Vélez-Málaga","combinedLocationIds":"724,1,29,323,561,29094,0,3552,0","coordinates":{"longitude":-4.10032,"latitude":36.77798},"text":"Vélez-Málaga ciudad, Vélez-Málaga","baseText":"Vélez-Málaga ciudad"},{"localizationLevel5":"","combinedLocationIds":"malaga-grinon","coordinates":{"longitude":-3.845498993283961,"latitude":40.2202423014089},"text":"Málaga, Griñón","baseText":"Málaga"},{"localizationLevel5":"","combinedLocationIds":"724,1,29,319,547,0,0,0,0","coordinates":{"longitude":-4.41862,"latitude":36.7217},"text":"Málaga, Zona de","baseText":"Málaga, Zona de"},{"localizationLevel5":"","combinedLocationIds":"malaga-villaviciosa-de-odon","coordinates":{"longitude":-3.91037376133964,"latitude":40.36042679552922},"text":"Málaga, Villaviciosa de Odón","baseText":"Málaga"},{"localizationLevel5":"Vélez-Málaga","combinedLocationIds":"724,1,29,323,561,29094,0,3552,1023","coordinates":{"longitude":-4.10634,"latitude":36.77581},"text":"Camino Viejo de Málaga, Vélez-Málaga","baseText":"Camino Viejo de Málaga"}]}""")


class TestSeleccionDeZona:
    """La respuesta real trae diez sugerencias, y elegir mal es fácil: la
    primera es la provincia entera y dos son municipios homónimos de Madrid,
    a más de 400 km."""

    def test_elige_el_municipio_y_no_la_provincia(self):
        from app.sources.rapidapi import select_location_id

        ident, detalle = select_location_id(RESPUESTA_SUGGESTIONS_MALAGA, 36.7213, -4.4214)
        assert ident == "724,1,29,319,547,29067,0,0,0"
        assert detalle["text"] == "Málaga, Málaga"
        assert detalle["depth"] == 6

    def test_no_elige_la_primera_sugerencia(self):
        """La primera es la provincia: 724,1,29,0,0,0,0,0,0."""
        from app.sources.rapidapi import select_location_id

        primera = RESPUESTA_SUGGESTIONS_MALAGA["data"][0]["combinedLocationIds"]
        ident, _ = select_location_id(RESPUESTA_SUGGESTIONS_MALAGA, 36.7213, -4.4214)
        assert ident != primera

    def test_descarta_los_homonimos_lejanos(self):
        """«Málaga, Griñón» y «Málaga, Villaviciosa de Odón» están en Madrid."""
        from app.sources.rapidapi import select_location_id

        ident, detalle = select_location_id(RESPUESTA_SUGGESTIONS_MALAGA, 36.7213, -4.4214)
        assert "grinon" not in str(ident)
        assert "villaviciosa" not in str(ident)
        # De diez sugerencias, sólo ocho caen cerca del punto.
        assert detalle["candidates_considered"] == 8

    def test_distingue_velez_malaga_de_malaga(self):
        """Mismo texto de búsqueda, punto distinto, zona distinta."""
        from app.sources.rapidapi import select_location_id

        ident, detalle = select_location_id(RESPUESTA_SUGGESTIONS_MALAGA, 36.784, -4.10077)
        assert ident == "724,1,29,323,561,29094,0,0,0"
        assert "Vélez" in detalle["text"]

    def test_rechaza_si_ninguna_sugerencia_esta_cerca(self):
        """Buscar «Málaga» desde Marbella no debe devolver la zona de Málaga."""
        from app.sources.rapidapi import select_location_id

        ident, detalle = select_location_id(RESPUESTA_SUGGESTIONS_MALAGA, 36.5101, -4.8825)
        assert ident is None and detalle == {}

    def test_prefiere_el_nivel_municipio_al_barrio(self):
        from app.sources.rapidapi import select_location_id

        # Junto a «Viña Málaga» (barrio, 8 niveles) gana Vélez-Málaga (6).
        ident, _ = select_location_id(RESPUESTA_SUGGESTIONS_MALAGA, 36.74332, -4.10236)
        assert ident == "724,1,29,323,561,29094,0,0,0"

    def test_el_nombre_real_del_campo_se_reconoce(self):
        """Es combinedLocationIds, no combinedLocations como decía la doc."""
        from app.sources.rapidapi import _find_combined_locations

        assert _find_combined_locations(
            {"combinedLocationIds": "1,2,3"}, deep=False
        ) == "1,2,3"

    def test_profundidad_de_un_identificador(self):
        from app.sources.rapidapi import _location_depth

        assert _location_depth("724,1,29,0,0,0,0,0,0") == 3         # provincia
        assert _location_depth("724,1,29,319,547,29067,0,0,0") == 6  # municipio
        assert _location_depth("724,1,29,323,561,29094,0,3552,1023") == 8  # barrio
        assert _location_depth("malaga-grinon") == 0                # slug, no jerárquico


class TestImagenDeReferencia:
    """La descarga la hace el servidor desplegado, que sí tiene red. Enlazar la
    imagen de la tienda daría fotos rotas: rechazan las peticiones cuyo Referer
    no es el suyo."""

    @staticmethod
    def _source():
        from app.sources.images import ReferenceImageSource

        return ReferenceImageSource()

    def test_extrae_og_image(self):
        html = '<meta property="og:image" content="https://cdn/foto.jpg">'
        assert self._source().extract_image_url(html, "https://tienda/x") == "https://cdn/foto.jpg"

    def test_extrae_og_image_con_atributos_invertidos(self):
        html = '<meta content="https://cdn/f.jpg" property="og:image">'
        assert self._source().extract_image_url(html, "https://t/x") == "https://cdn/f.jpg"

    def test_cae_a_twitter_image(self):
        html = '<meta name="twitter:image" content="https://cdn/tw.jpg">'
        assert self._source().extract_image_url(html, "https://t/x") == "https://cdn/tw.jpg"

    def test_resuelve_rutas_relativas(self):
        html = '<meta property="og:image" content="/img/casa.jpg">'
        resultado = self._source().extract_image_url(html, "https://tienda.es/producto/1")
        assert resultado == "https://tienda.es/img/casa.jpg"

    def test_desescapa_entidades(self):
        html = '<meta property="og:image" content="https://cdn/f.jpg?a=1&amp;b=2">'
        assert self._source().extract_image_url(html, "https://t/") == "https://cdn/f.jpg?a=1&b=2"

    def test_sin_imagen_declarada(self):
        assert self._source().extract_image_url("<html></html>", "https://t/") is None

    def test_rechaza_urls_que_no_son_http(self):
        from app.sources.base import SourceError

        with pytest.raises(SourceError, match="http"):
            self._source().fetch("file:///etc/passwd")

    def test_descarga_directa_de_una_imagen(self, monkeypatch):
        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200, content=b"binario", headers={"content-type": "image/jpeg"},
                request=httpx.Request(method, url),
            ),
        )
        contenido, extension = self._source().fetch("https://cdn/foto.jpg")
        assert contenido == b"binario" and extension == ".jpg"

    def test_dos_pasos_pagina_y_luego_imagen(self, monkeypatch):
        """Lo habitual: la URL es la del anuncio, no la de la foto."""
        def fake(self, method, url, **kw):
            if url.endswith(".webp"):
                return httpx.Response(
                    200, content=b"foto", headers={"content-type": "image/webp"},
                    request=httpx.Request(method, url),
                )
            return httpx.Response(
                200, text='<meta property="og:image" content="https://cdn/p.webp">',
                headers={"content-type": "text/html"},
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.Client, "request", fake)
        contenido, extension = self._source().fetch("https://tienda.es/producto/1")
        assert contenido == b"foto" and extension == ".webp"

    def test_explica_el_bloqueo_de_la_tienda(self, monkeypatch):
        """Un 403 al descargar la imagen es el caso más común y frecuente."""
        from app.sources.base import SourceError

        def fake(self, method, url, **kw):
            if url.endswith(".jpg"):
                return httpx.Response(403, request=httpx.Request(method, url))
            return httpx.Response(
                200, text='<meta property="og:image" content="https://cdn/x.jpg">',
                headers={"content-type": "text/html"},
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.Client, "request", fake)
        with pytest.raises(SourceError) as error:
            self._source().fetch("https://tienda.es/p/1")
        assert "403" in str(error.value)
        assert "a mano" in str(error.value)     # dice qué hacer, no sólo que falló

    def test_rechaza_tipos_que_no_son_imagen(self, monkeypatch):
        from app.sources.base import SourceError

        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200, content=b"<svg/>", headers={"content-type": "image/svg+xml"},
                request=httpx.Request(method, url),
            ),
        )
        with pytest.raises(SourceError, match="no admitido"):
            self._source().fetch("https://cdn/x.svg")

    def test_un_bloqueo_de_red_no_provoca_un_500(self, monkeypatch):
        """Escapaba como httpx.ProxyError y el endpoint devolvía 500.

        Cualquier fallo de red debe salir como SourceError, que es lo único
        que la capa web espera atrapar.
        """
        from app.sources.base import SourceError

        def fake(self, method, url, **kw):
            raise httpx.ProxyError("403 Forbidden")

        monkeypatch.setattr(httpx.Client, "request", fake)
        with pytest.raises(SourceError) as error:
            self._source().fetch("https://www.amazon.es/dp/X")
        assert "amazon.es" in str(error.value)

    def test_un_fallo_de_conexion_tampoco(self, monkeypatch):
        from app.sources.base import SourceError

        def fake(self, method, url, **kw):
            raise httpx.ConnectError("nombre no resuelto")

        monkeypatch.setattr(httpx.Client, "request", fake)
        with pytest.raises(SourceError, match="No se pudo acceder"):
            self._source().fetch("https://tienda.invalida/p")

    def test_manda_referer_para_no_ser_bloqueado(self, monkeypatch):
        cabeceras = {}

        def fake(self, method, url, **kw):
            cabeceras.update(kw.get("headers") or {})
            return httpx.Response(
                200, content=b"x", headers={"content-type": "image/png"},
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(httpx.Client, "request", fake)
        self._source().fetch("https://tienda.es/p/1")
        assert cabeceras.get("Referer") == "https://tienda.es/p/1"


class TestMiniaturaDelAnuncio:
    """Las APIs ya devuelven la foto del anuncio; descartarla era desaprovecharla."""

    def test_idealista_conserva_la_miniatura(self):
        from app.sources.idealista import IdealistaSource

        item = {"propertyCode": "1", "price": 1000, "size": 500,
                "latitude": 36.7, "longitude": -4.4,
                "thumbnail": "https://img.idealista.com/foto.jpg"}
        resultado = IdealistaSource.normalize(item)
        assert resultado["raw"]["thumbnail_url"] == "https://img.idealista.com/foto.jpg"

    def test_rapidapi_busca_la_foto_en_varios_campos(self):
        from app.sources.rapidapi import RapidApiIdealista17Source

        source = RapidApiIdealista17Source()
        for campo in ("thumbnail", "image", "mainImage", "photo"):
            item = {"propertyCode": "1", "price": 1000, "size": 500,
                    campo: "https://cdn/f.jpg"}
            assert source.normalize(item)["raw"]["thumbnail_url"] == "https://cdn/f.jpg", campo

    def test_encuentra_la_foto_dentro_de_una_lista(self):
        """Varios proveedores anidan las fotos en un array."""
        from app.sources.rapidapi import RapidApiIdealista17Source

        item = {"propertyCode": "1", "price": 1000, "size": 500,
                "images": [{"url": "https://cdn/primera.jpg"},
                           {"url": "https://cdn/segunda.jpg"}]}
        resultado = RapidApiIdealista17Source().normalize(item)
        assert resultado["raw"]["thumbnail_url"] == "https://cdn/primera.jpg"

    def test_sin_foto_queda_vacio_y_no_rompe(self):
        from app.sources.rapidapi import RapidApiIdealista17Source

        item = {"propertyCode": "1", "price": 1000, "size": 500}
        assert RapidApiIdealista17Source().normalize(item)["raw"]["thumbnail_url"] == ""


class TestSubastasBoe:
    """Única fuente gratuita de ofertas activas en toda España. Es información
    del sector público, no scraping de un portal privado."""

    DETALLE = """
    <table>
     <tr><th>Valor subasta</th><td>48.500,00 €</td></tr>
     <tr><th>Tasación</th><td>97.000,00 €</td></tr>
     <tr><th>Puja mínima</th><td>24.250,00 €</td></tr>
    </table>
    <table>
     <tr><th>Descripción</th><td>Finca rústica de 1,25 hectáreas en Noja.</td></tr>
     <tr><th>Tipo de bien</th><td>Finca rústica</td></tr>
     <tr><th>Localidad</th><td>Noja</td></tr>
     <tr><th>Provincia</th><td>Cantabria</td></tr>
     <tr><th>Referencia catastral</th><td>39047A00700123 0000XY</td></tr>
    </table>"""

    @staticmethod
    def _source():
        from app.sources.boe import BoeSubastasSource

        return BoeSubastasSource

    def test_lee_los_pares_etiqueta_valor(self):
        detalle = self._source().parse_detail(self.DETALLE, "SUB-JA-2026-1")
        assert detalle["asset_type"] == "Finca rústica"
        assert detalle["municipality"] == "Noja"
        assert detalle["province"] == "Cantabria"

    def test_el_precio_es_la_puja_minima_no_la_tasacion(self):
        """Es lo que hay que pagar; usar la tasación inflaría el €/m²."""
        detalle = self._source().parse_detail(self.DETALLE, "SUB-JA-2026-1")
        item = self._source().normalize(detalle)
        assert item["price_eur"] == 24250.0

    def test_convierte_hectareas_a_metros(self):
        """Confundirlas cambia el precio por metro en cuatro órdenes."""
        detalle = self._source().parse_detail(self.DETALLE, "SUB-JA-2026-1")
        item = self._source().normalize(detalle)
        assert item["area_m2"] == 12500.0
        assert item["price_eur_m2"] == pytest.approx(1.94, abs=0.01)

    @pytest.mark.parametrize("texto,esperado", [
        ("1,25 hectáreas", 12500.0),
        ("2 Has", 20000.0),
        ("45 áreas", 4500.0),
        ("1.250 m2", 1250.0),
        ("800 m²", 800.0),
        ("3.500 metros cuadrados", 3500.0),
        ("sin superficie", None),
    ])
    def test_unidades_de_superficie(self, texto, esperado):
        from app.sources.boe import _parse_area

        resultado = _parse_area(texto)
        if esperado is None:
            assert resultado is None
        else:
            assert resultado == pytest.approx(esperado)

    def test_limpia_la_referencia_catastral(self):
        detalle = self._source().parse_detail(self.DETALLE, "SUB-JA-2026-1")
        item = self._source().normalize(detalle)
        assert item["cadastral_ref"] == "39047A007001230000XY"
        assert len(item["cadastral_ref"]) == 20

    def test_distingue_suelo_de_vivienda(self):
        source = self._source()
        suelo = source.parse_detail(self.DETALLE, "x")
        assert source.is_land(suelo)
        piso = {"asset_type": "Vivienda", "description": "Piso de 90 m2 con plaza de garaje"}
        assert not source.is_land(piso)

    def test_extrae_identificadores_sin_duplicar(self):
        html = ('<a href="detalleSubasta.php?idSub=SUB-JA-2026-1">a</a>'
                '<a href="detalleSubasta.php?idSub=SUB-NE-2026-2">b</a>'
                '<a href="detalleSubasta.php?idSub=SUB-JA-2026-1">repetida</a>')
        assert self._source().parse_result_ids(html) == ["SUB-JA-2026-1", "SUB-NE-2026-2"]

    def test_codigos_de_provincia(self):
        source = self._source()
        assert source.province_code("Cantabria") == "39"
        assert source.province_code("cantabria") == "39"
        assert source.province_code("Málaga") == "29"
        assert source.province_code("7") == "07"       # se rellena a dos dígitos
        assert source.province_code(None) is None
        assert source.province_code("Provincia inventada") is None

    def test_sin_importe_o_sin_superficie_se_descarta(self):
        source = self._source()
        assert source.normalize({"id_sub": "x", "url": "u", "description": "sin datos"}) is None

    def test_aparece_en_el_registro_de_fuentes(self):
        from app.sources.registry import build_sources

        assert "boe_subastas" in {s.key for s in build_sources()}

    def test_prueba_varias_estrategias_de_busqueda(self):
        """La forma exacta de la búsqueda del portal no está documentada, así
        que se prueban varias en vez de fijar una sola y darla por buena."""
        estrategias = self._source()().search_strategies("Cantabria", 40)
        nombres = [n for n, _, _, _ in estrategias]
        assert len(nombres) >= 3
        # Las sondas sin filtro van al final: si ahí tampoco sale nada, el
        # problema no son los parámetros sino el acceso o el parseo.
        assert nombres[-2:] == ["sin-filtros", "listado-sin-filtros"]

    def test_se_prueba_tambien_por_post(self):
        """El formulario del portal es un POST; que acepte GET no está dicho."""
        estrategias = self._source()().search_strategies("Cantabria", 40)
        metodos = {metodo for _, metodo, _, _ in estrategias}
        assert metodos == {"GET", "POST"}

    def test_la_provincia_va_en_el_campo_que_dice_el_formulario(self):
        """El portal la espera en dato[8]; se venía mandando en dato[2]."""
        params = {n: p for n, _, _, p in self._source()().search_strategies("Cantabria", 40)}
        assert params["a-mano-get"]["dato[8]"] == "39"

    def test_sin_provincia_no_se_filtra_por_ella(self):
        params = {n: p for n, _, _, p in self._source()().search_strategies(None, 40)}
        assert "dato[8]" not in params["a-mano-get"]

    def test_solo_se_mandan_valores_que_el_portal_admite(self):
        """page_hits admite 50, 100, 200 y 500; se mandaba 25 y 40."""
        params = {n: p for n, _, _, p in self._source()().search_strategies("Cantabria", 25)}
        a_mano = params["a-mano-get"]
        assert a_mano["page_hits"] in (50, 100, 200, 500)
        # SUBASTA.FECHA_FIN_YMD no está entre las opciones de ordenación.
        assert a_mano["sort_field[0]"] == "SUBASTA.FECHA_FIN"

    def test_se_queda_con_la_primera_estrategia_que_devuelve_algo(self, monkeypatch):
        from app.sources.boe import BoeSubastasSource

        source = BoeSubastasSource()
        llamadas = {"n": 0}

        def fake(self, method, url, **kw):
            llamadas["n"] += 1
            # La primera llamada es la que abre sesión; acierta la siguiente.
            cuerpo = ('<a href="detalleSubasta.php?idSub=SUB-JA-2026-1">x</a>'
                      if llamadas["n"] == 2 else "<html>sin resultados</html>")
            return httpx.Response(200, text=cuerpo, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake)
        assert source.search("Cantabria") == ["SUB-JA-2026-1"]
        assert llamadas["n"] == 2      # no sigue probando una vez acierta

    def test_cuando_nada_funciona_informa_de_cada_intento(self, monkeypatch):
        """Decir «0 subastas» sin más no permite saber qué falló."""
        from app.sources.boe import BoeSubastasSource

        source = BoeSubastasSource()
        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200, text="<html><body><form>Buscador de subastas</form></body></html>",
                request=httpx.Request(method, url),
            ),
        )
        intentos = source.search_attempts("Cantabria")
        nombres = [n for n, _, _ in intentos]
        assert nombres[0] == "sesion-inicial"   # primero se abre la sesión
        assert len(intentos) >= 4               # y después todas las estrategias
        for nombre, ids, info in intentos[1:]:
            assert ids == []
            assert info["http_status"] == 200
            assert info["link_patterns"]["formulario"] == 1
            assert "Buscador de subastas" in info["body_excerpt"]

    def test_el_extracto_quita_etiquetas_y_scripts(self):
        from app.sources.boe import _excerpt

        html = "<html><script>var a=1;</script><body><p>Texto  visible</p></body></html>"
        assert _excerpt(html) == "Texto visible"

    def test_reconoce_el_identificador_en_texto_suelto(self):
        """No todos los listados lo ponen dentro de un enlace."""
        assert self._source().parse_result_ids(
            "referencia SUB-NE-2026-000987 publicada"
        ) == ["SUB-NE-2026-000987"]


class TestCatastroConRespaldo:
    """Un punto puede caer en un vial, en marisma o en dominio público y no
    tener parcela, aunque haya suelo alrededor. Antes eso dejaba la simulación
    sin geometría real; ahora se busca en el entorno."""

    OK = """<consulta_coordenadas><control><cucoor>1</cucoor><cuerr>0</cuerr></control>
      <coordenadas><coord><pc><pc1>39047A007</pc1><pc2>001230000XY</pc2></pc>
      <ldt>NOJA</ldt></coord></coordenadas></consulta_coordenadas>"""

    VACIO = ("<consulta_coordenadas><control><cucoor>0</cucoor><cuerr>0</cuerr>"
             "</control></consulta_coordenadas>")

    ERROR = """<consulta_coordenadas><control><cucoor>0</cucoor><cuerr>1</cuerr></control>
      <lerr><err><cod>13</cod><des>EL SRS NO ES VALIDO</des></err></lerr>
      </consulta_coordenadas>"""

    CERCANAS = """<consulta_coordenadas_distancias><coordd><lpcd>
      <pcd><pc><pc1>39047A007</pc1><pc2>001230000XY</pc2></pc><dis>12</dis><ldt>A</ldt></pcd>
      <pcd><pc><pc1>39047A007</pc1><pc2>001240000XZ</pc2></pc><dis>4</dis><ldt>B</ldt></pcd>
      </lpcd></coordd></consulta_coordenadas_distancias>"""

    def test_lee_la_referencia(self):
        assert CatastroSource.parse_ref(self.OK) == "39047A007001230000XY"

    def test_un_punto_sin_parcela_devuelve_none(self):
        assert CatastroSource.parse_ref(self.VACIO) is None

    def test_un_error_dentro_de_un_200_no_pasa_por_falta_de_parcela(self):
        """El servicio responde 200 con el motivo en el cuerpo. Ignorarlo hacía
        que un fallo de parámetros pareciera «aquí no hay parcela»."""
        from app.sources.base import SourceError

        with pytest.raises(SourceError, match="SRS NO ES VALIDO"):
            CatastroSource.parse_ref(self.ERROR)

    def test_las_cercanas_salen_ordenadas_por_distancia(self):
        parcelas = CatastroSource.parse_refs_near(self.CERCANAS)
        assert [p["distance_m"] for p in parcelas] == [4.0, 12.0]
        assert parcelas[0]["cadastral_ref"] == "39047A007001240000XZ"

    def test_sin_parcelas_cercanas(self):
        assert CatastroSource.parse_refs_near(
            "<consulta_coordenadas_distancias/>") == []

    def test_recurre_a_la_cercana_cuando_el_punto_no_tiene_parcela(self, monkeypatch):
        source = CatastroSource()

        def fake(self, method, url, **kw):
            if "Consulta_RCCOOR_Distancia" in url:
                cuerpo = TestCatastroConRespaldo.CERCANAS
            elif "Consulta_RCCOOR" in url:
                cuerpo = TestCatastroConRespaldo.VACIO
            else:   # el WFS de geometría
                cuerpo = ('<FeatureCollection><posList>43.4 -3.5 43.5 -3.5 43.5 -3.4'
                          '</posList></FeatureCollection>')
            return httpx.Response(200, text=cuerpo, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake)
        detalle = source.parcel_at_detail(43.4869, -3.5290)
        assert detalle["used_nearby"] is True
        assert detalle["distance_m"] == 4.0
        assert detalle["cadastral_ref"] == "39047A007001240000XZ"

    def test_informa_de_cada_paso_intentado(self, monkeypatch):
        """«No hay parcela» podía significar tres cosas distintas."""
        source = CatastroSource()
        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200,
                text=(TestCatastroConRespaldo.CERCANAS
                      if "Distancia" in url else TestCatastroConRespaldo.VACIO),
                request=httpx.Request(method, url),
            ),
        )
        detalle = source.parcel_at_detail(43.4869, -3.5290)
        pasos = [p["step"] for p in detalle["steps"]]
        assert pasos == ["exacta", "cercanas"]
        assert detalle["steps"][1]["found"] == 2

    def test_cuando_no_hay_nada_explica_el_motivo(self, monkeypatch):
        source = CatastroSource()
        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200,
                text=("<consulta_coordenadas_distancias/>" if "Distancia" in url
                      else TestCatastroConRespaldo.VACIO),
                request=httpx.Request(method, url),
            ),
        )
        detalle = source.parcel_at_detail(0.0, 0.0)
        assert detalle["feature"] is None
        assert "agua" in detalle["reason"] or "vial" in detalle["reason"]


class TestIneTurismo:
    """InsideAirbnb no cubre España; el INE sí, y por la misma API ya usada."""

    def test_separa_territorio_e_indicador(self):
        from app.sources.tourism import IneTourismSource

        crudo = [
            {"Nombre": "Noja. Viviendas turísticas. ",
             "Data": [{"Anyo": 2025, "FK_Periodo": 11, "Valor": 412.0}]},
            {"Nombre": "Noja. Plazas. ",
             "Data": [{"Anyo": 2025, "FK_Periodo": 11, "Valor": 2130.0}]},
        ]
        parseado = IneTourismSource.parse_housing(crudo)
        assert parseado["Noja"]["dwellings"] == 412.0
        assert parseado["Noja"]["beds"] == 2130.0
        assert parseado["Noja"]["period"] == 2025

    def test_se_queda_con_el_periodo_mas_reciente(self):
        from app.sources.tourism import IneTourismSource

        crudo = [{"Nombre": "Llanes. Plazas. ", "Data": [
            {"Anyo": 2024, "FK_Periodo": 5, "Valor": 900.0},
            {"Anyo": 2025, "FK_Periodo": 11, "Valor": 1200.0},
        ]}]
        assert IneTourismSource.parse_housing(crudo)["Llanes"]["beds"] == 1200.0

    def test_una_serie_sin_indicador_conocido_se_ignora(self):
        """El INE mete indicadores que no interesan en la misma tabla."""
        from app.sources.tourism import IneTourismSource

        crudo = [{"Nombre": "Noja. Otro indicador cualquiera. ",
                  "Data": [{"Anyo": 2025, "Valor": 1.0}]}]
        assert IneTourismSource.parse_housing(crudo) == {}

    def test_una_serie_sin_datos_no_rompe(self):
        from app.sources.tourism import IneTourismSource

        crudo = [{"Nombre": "Noja. Plazas. ", "Data": []},
                 {"Nombre": "Noja. Viviendas turísticas. ",
                  "Data": [{"Anyo": 2025, "Valor": None}]}]
        assert IneTourismSource.parse_housing(crudo) == {}


class TestEstimacionTuristicaPorIntensidad:
    """Sin datos de precio, la intensidad turística al menos distingue zonas."""

    def _municipio(self, plazas, viviendas, poblacion):
        from app.models import Municipality

        return Municipality(
            ine_code="39047", name="Noja", province="Cantabria", ccaa="Cantabria",
            lat=43.48, lon=-3.53, population=poblacion,
            tourist_beds=plazas, tourist_dwellings=viviendas,
            tourist_data_period="2025",
        )

    def test_un_destino_turistico_sale_por_encima_del_respaldo(self):
        from app.services import FALLBACK_ADR_EUR, _rental_from_ine

        estimacion = _rental_from_ine(self._municipio(2130, 412, 2600))
        assert estimacion["adr_eur"] > FALLBACK_ADR_EUR
        assert estimacion["source"] == "ine_turismo"
        # No es un precio observado y la ficha no debe presentarlo como tal.
        assert estimacion["is_real_data"] is False
        assert "plazas" in estimacion["basis"]

    def test_un_pueblo_sin_turismo_no_recibe_estimacion(self):
        from app.services import _rental_from_ine

        assert _rental_from_ine(self._municipio(2, 1, 4000)) is None

    def test_sin_dato_del_ine_no_inventa_nada(self):
        from app.services import _rental_from_ine

        assert _rental_from_ine(self._municipio(None, None, 2600)) is None

    def test_una_ciudad_grande_no_pasa_por_destino(self):
        """Mil plazas en Madrid no son lo mismo que mil en Noja."""
        from app.services import _rental_from_ine

        ciudad = self._municipio(1000, 300, 500000)
        assert _rental_from_ine(ciudad) is None


class TestFormularioDelBoe:
    """Los parámetros del portal no están documentados: se leen del HTML."""

    def test_saca_los_codigos_que_admite_cada_campo(self):
        from app.sources.boe import parse_form

        formulario = parse_form(
            '<form action="subastas_ava.php" method="get">'
            '<select name="dato[8]">'
            '<option value="">Todas</option>'
            '<option value="39">Cantabria</option>'
            '<option value="45">Toledo</option>'
            "</select>"
            '<input type="hidden" name="campo[8]" value="BIEN.PROVINCIA">'
            '<input type="submit" name="accion" value="Buscar"></form>'
        )
        campo = formulario["fields"]["dato[8]"]
        assert [o["value"] for o in campo["options"]] == ["", "39", "45"]
        assert campo["options"][1]["label"] == "Cantabria"
        # El valor de los campos ocultos importa: es lo que empareja cada
        # dato[N] con el campo al que se refiere.
        assert formulario["fields"]["campo[8]"]["value"] == "BIEN.PROVINCIA"
        assert formulario["submits"] == {"accion": "Buscar"}
        assert formulario["action"] == "subastas_ava.php"


class TestSesionDelBoe:
    """El portal entrega su cookie en el formulario y la exige al buscar."""

    def test_todas_las_peticiones_comparten_cliente(self, monkeypatch):
        """Un cliente nuevo por petición hacía llegar la búsqueda sin sesión."""
        import httpx

        from app.sources.boe import BoeSubastasSource

        clientes: list[int] = []

        def fake(self, method, url, **kw):
            clientes.append(id(self))
            return httpx.Response(200, text="<html>nada</html>",
                                  request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake)
        BoeSubastasSource().search_attempts("Cantabria")
        assert len(clientes) >= 4
        assert len(set(clientes)) == 1      # una sola sesión para todo

    def test_se_carga_el_formulario_antes_de_buscar(self, monkeypatch):
        import httpx

        from app.sources.boe import BoeSubastasSource

        vistas: list[tuple[str, str]] = []

        def fake(self, method, url, **kw):
            vistas.append((method, str(url)))
            return httpx.Response(200, text="<html>nada</html>",
                                  request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake)
        intentos = BoeSubastasSource().search_attempts("Cantabria")

        # La primera es la de calentamiento, sin parámetros de búsqueda.
        assert vistas[0][0] == "GET" and "accion" not in vistas[0][1]
        assert intentos[0][0] == "sesion-inicial"
        assert "cookies" in intentos[0][2]

    def test_la_sesion_recuerda_la_cookie(self, monkeypatch):
        import httpx

        from app.sources.boe import BoeSubastasSource

        enviadas: list[str] = []

        def fake(self, method, url, **kw):
            enviadas.append(self.cookies.get("PHPSESSID") or "")
            respuesta = httpx.Response(200, text="<html>nada</html>",
                                       request=httpx.Request(method, url))
            self.cookies.set("PHPSESSID", "abc123")
            return respuesta

        monkeypatch.setattr(httpx.Client, "request", fake)
        BoeSubastasSource().search_attempts("Cantabria")
        # La primera va sin cookie; a partir de ahí la lleva.
        assert enviadas[0] == ""
        assert all(c == "abc123" for c in enviadas[1:])


class TestReplicaDelFormularioDelBoe:
    """Adivinar los parámetros fue lo que hizo devolver cero durante días.

    El formulario de aquí es el real del portal, recortado: mismos nombres de
    campo y mismos códigos que devolvió el diagnóstico en producción.
    """

    FORMULARIO = (
        '<form action="subastas_ava.php" method="get">'
        '<input type="hidden" name="campo[0]" value="SUBASTA.ESTADO">'
        '<select name="dato[0]"><option value="EJ" selected>Celebrándose</option>'
        '<option value="PU">Publicada</option></select>'
        '<input type="hidden" name="campo[8]" value="BIEN.PROVINCIA">'
        '<select name="dato[8]">'
        '<option value="">-- Todas --</option>'
        '<option value="08">Barcelona</option>'
        '<option value="28">Madrid</option>'
        '<option value="39">Cantabria</option>'
        '<option value="46">Valencia/València</option>'
        "</select>"
        '<select name="page_hits"><option value="50">50</option>'
        '<option value="100">100</option><option value="500">500</option></select>'
        '<select name="sort_field[0]">'
        '<option value="SUBASTA.FECHA_FIN">Fecha fin subasta</option>'
        '<option value="SUBASTA.ESTADO.CODIGO">Estado</option></select>'
        '<select name="sort_order[0]"><option value="desc">descendente</option>'
        '<option value="asc">ascendente</option></select>'
        '<input type="submit" name="accion" value="Buscar">'
        "</form>"
    )

    def _form(self):
        from app.sources.boe import parse_form

        return parse_form(self.FORMULARIO)

    def test_la_provincia_se_reconoce_por_sus_codigos(self):
        """Hoy está en dato[8], pero ese número es maquetación y puede cambiar."""
        from app.sources.boe import province_slot

        assert province_slot(self._form()) == "dato[8]"

    def test_se_envian_los_campos_ocultos_del_formulario(self):
        """Sin campo[8], mandar dato[8]=39 no le dice nada al portal."""
        from app.sources.boe import search_params_from_form

        params = search_params_from_form(self._form(), "39", 50)
        assert params["campo[8]"] == "BIEN.PROVINCIA"
        assert params["dato[8]"] == "39"

    def test_se_conserva_lo_que_el_formulario_trae_marcado(self):
        from app.sources.boe import search_params_from_form

        params = search_params_from_form(self._form(), "39", 50)
        assert params["campo[0]"] == "SUBASTA.ESTADO"
        assert params["dato[0]"] == "EJ"        # la opción marcada por defecto

    def test_un_tamano_de_pagina_no_admitido_se_sustituye(self):
        from app.sources.boe import search_params_from_form

        params = search_params_from_form(self._form(), "39", 25)
        assert params["page_hits"] == "50"

    def test_un_tamano_de_pagina_admitido_se_respeta(self):
        from app.sources.boe import search_params_from_form

        assert search_params_from_form(self._form(), "39", 100)["page_hits"] == "100"

    def test_sin_provincia_no_se_manda_el_campo(self):
        from app.sources.boe import search_params_from_form

        assert "dato[8]" not in search_params_from_form(self._form(), None, 50)

    def test_se_manda_el_boton_de_buscar(self):
        """El portal distingue pintar el formulario de ejecutar la búsqueda."""
        from app.sources.boe import search_params_from_form

        assert search_params_from_form(self._form(), "39", 50)["accion"] == "Buscar"

    def test_replicar_el_formulario_va_antes_que_adivinar(self, monkeypatch):
        import httpx

        from app.sources.boe import BoeSubastasSource

        enviados: list[dict] = []

        formulario = self.FORMULARIO

        def fake(_cliente, method, url, **kw):
            enviados.append(kw.get("params") or kw.get("data") or {})
            # La primera petición es la que carga el formulario.
            cuerpo = formulario if len(enviados) == 1 else "<html>nada</html>"
            return httpx.Response(200, text=cuerpo, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", fake)
        intentos = BoeSubastasSource().search_attempts("Cantabria")
        nombres = [n for n, _, _ in intentos]
        assert nombres[1] == "formulario-get"
        # Y la primera búsqueda real ya lleva los campos que pide el portal.
        assert enviados[1]["campo[8]"] == "BIEN.PROVINCIA"
        assert enviados[1]["dato[8]"] == "39"

    def test_si_no_hay_formulario_se_sigue_intentando_a_mano(self, monkeypatch):
        """Que el portal deje de servir el formulario no puede dejarnos sin nada."""
        import httpx

        from app.sources.boe import BoeSubastasSource

        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200, text="<html>sin formulario</html>",
                request=httpx.Request(method, url),
            ),
        )
        nombres = [n for n, _, _ in BoeSubastasSource().search_attempts("Cantabria")]
        assert "a-mano-get" in nombres and "sin-filtros" in nombres


class TestApiDeSumariosDelBoe:
    """La vía documentada: el buscador del portal no es una API.

    Toda subasta se anuncia en el Boletín antes de celebrarse, así que el
    sumario diario las trae todas sin depender de adivinar parámetros.
    """

    SUMARIO = {"data": {"sumario": {"diario": [{"numero": "217", "seccion": [
        {"codigo": "4", "nombre": "IV. Administración de Justicia",
         "departamento": [{"nombre": "JUZGADOS DE PRIMERA INSTANCIA",
                           "epigrafe": [{"nombre": "SANTANDER", "item": [
                               {"identificador": "BOE-B-2026-1",
                                "titulo": "Anuncio de subasta de finca rústica en Noja",
                                "url_xml": "/diario_boe/xml.php?id=BOE-B-2026-1"},
                               {"identificador": "BOE-B-2026-2",
                                "titulo": "Edicto de notificación de sentencia"},
                           ]}]}]},
        {"codigo": "5", "nombre": "V. Anuncios",
         "departamento": [{"nombre": "AEAT", "item": [
             {"identificador": "BOE-B-2026-3",
              "titulo": "Anuncio de subasta de bienes inmuebles"}]}]},
    ]}]}}}

    def _source(self):
        from app.sources.boe_api import BoeSumarioSource

        return BoeSumarioSource()

    def test_encuentra_los_anuncios_de_subasta(self):
        anuncios = self._source().auction_items(self.SUMARIO)
        assert [a["identificador"] for a in anuncios] == ["BOE-B-2026-1", "BOE-B-2026-3"]

    def test_deja_fuera_lo_que_no_es_una_subasta(self):
        titulos = [a["titulo"] for a in self._source().auction_items(self.SUMARIO)]
        assert not any("Edicto" in t for t in titulos)

    def test_recorre_el_sumario_sin_depender_de_su_anidacion(self):
        """No todos los boletines traen los mismos niveles: hay días sin
        epígrafe. Buscar por una ruta fija se rompería con el primero."""
        plano = {"item": [{"identificador": "BOE-B-2026-9",
                           "titulo": "Anuncio de subasta notarial"}]}
        assert len(self._source().auction_items(plano)) == 1

    def test_las_urls_relativas_se_completan(self):
        anuncio = self._source().auction_items(self.SUMARIO)[0]
        assert anuncio["url_xml"].startswith("https://www.boe.es/")

    def test_sin_url_en_el_sumario_se_construye(self):
        anuncio = self._source().auction_items(self.SUMARIO)[1]
        assert anuncio["url_xml"].endswith("id=BOE-B-2026-3")

    def test_saca_el_numero_de_subasta_del_texto_del_anuncio(self):
        from app.sources.boe_api import BoeSumarioSource

        texto = ("Se anuncia la subasta con identificador SUB-JA-2026-123456, "
                 "y en el mismo procedimiento SUB-JA-2026-123457.")
        assert BoeSumarioSource.auction_ids(texto) == [
            "SUB-JA-2026-123456", "SUB-JA-2026-123457"
        ]

    def test_un_dia_sin_boletin_no_es_un_error(self, monkeypatch):
        """Domingos y festivos no se publica: un 404 ahí es lo normal."""
        import httpx

        from app.sources.boe_api import BoeSumarioSource

        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                404, request=httpx.Request(method, url)),
        )
        from datetime import date

        assert BoeSumarioSource().sumario(date(2026, 9, 6)) is None

    def test_un_dia_roto_no_corta_el_recorrido(self, monkeypatch):
        import httpx

        from app.sources.boe_api import BoeSumarioSource

        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                500, text="", request=httpx.Request(method, url)),
        )
        from datetime import date

        hallazgo = BoeSumarioSource().recent_auction_ids(
            days=3, today=date(2026, 9, 8))
        assert len(hallazgo["days"]) == 3
        assert all("error" in d for d in hallazgo["days"])


class TestSinSubastasRepetidas:
    """La API de sumarios y el portal devuelven los mismos lotes."""

    def test_una_subasta_en_dos_fuentes_sale_una_vez(self):
        from app.discovery import _sin_repetidas

        lote = {"source": "boe_subastas", "external_id": "SUB-1", "price_eur": 1.0}
        assert len(_sin_repetidas([lote, dict(lote), {**lote, "external_id": "SUB-2"}])) == 2

    def test_gana_la_fuente_que_se_consulta_antes(self):
        from app.discovery import _sin_repetidas

        primera = {"source": "boe_subastas", "external_id": "SUB-1", "title": "de la API"}
        segunda = {"source": "boe_subastas", "external_id": "SUB-1", "title": "del portal"}
        assert _sin_repetidas([primera, segunda])[0]["title"] == "de la API"


class TestAnunciosDeSueloDelBoe:
    """El caso que dejaba el descubrimiento a cero.

    El sumario encontraba más de cien anuncios de subasta al día y no salía
    ninguna candidata. No fallaba la red: el camino era
    `sumario -> identificador SUB- -> ficha del portal`, y sólo llevan `SUB-`
    las subastas electrónicas. El INVIED, ADIF, SEPES y los ayuntamientos
    venden por pliego, sin ese identificador, y eran justo los que más suelo
    sacan. Aquí se leen del propio anuncio.
    """

    # Un anuncio real del INVIED en su forma habitual: varios lotes, mezcla de
    # suelo y vivienda, e importes que no son el precio de salida.
    XML = """<?xml version="1.0" encoding="UTF-8"?>
<documento>
 <metadatos>
  <identificador>BOE-B-2026-23129</identificador>
  <departamento codigo="4141">MINISTERIO DE DEFENSA</departamento>
  <seccion codigo="5">V. Anuncios</seccion>
  <titulo>Resoluci&#243;n del INVIED por la que se anuncian subastas p&#250;blicas,
  con proposici&#243;n econ&#243;mica al alza en sobre cerrado, de propiedades sitas
  en varias zonas de Espa&#241;a.</titulo>
  <fecha_publicacion>20260905</fecha_publicacion>
 </metadatos>
 <texto>
  <p>Se anuncia la enajenaci&#243;n de las siguientes propiedades.</p>
  <p>Lote n&#186; 1: Solar sito en el t&#233;rmino municipal de Le&#243;n, con una
  superficie de 2.340 m2, referencia catastral 9872023VH5797S0001WX.
  Tipo de licitaci&#243;n: 468.000,00 euros. Fianza: 23.400,00 euros.</p>
  <p>Lote n&#186; 2: Vivienda sita en Madrid de 90 metros cuadrados.
  Tipo de licitaci&#243;n: 310.000,00 euros.</p>
  <p>Lote n&#186; 3: Parcela r&#250;stica en el t&#233;rmino municipal de Almazora,
  provincia de Castell&#243;n, de 3,5 hect&#225;reas.
  Tipo de licitaci&#243;n: 87.500,00 euros.</p>
 </texto>
</documento>"""

    def _fuente(self):
        from app.sources.boe_anuncios import BoeAnunciosSource

        return BoeAnunciosSource()

    def _anuncio(self):
        return self._fuente().parse_xml(self.XML, "BOE-B-2026-23129")

    def test_lee_los_metadatos_del_anuncio(self):
        anuncio = self._anuncio()
        assert anuncio["departamento"] == "MINISTERIO DE DEFENSA"
        assert anuncio["seccion"] == "V. Anuncios"
        assert "INVIED" in anuncio["titulo"]

    def test_un_anuncio_sin_identificador_de_subasta_si_da_candidatas(self):
        """Es el fallo que se corrige: no lleva SUB- y antes se perdía entero."""
        from app.sources.boe_api import BoeSumarioSource

        assert BoeSumarioSource.auction_ids(self._anuncio()["texto"]) == []
        assert self._fuente().candidates(self._anuncio())

    def test_cada_lote_es_una_candidata(self):
        lotes = [c["raw"]["lote"] for c in self._fuente().candidates(self._anuncio())]
        assert lotes == ["1", "3"]

    def test_el_lote_de_vivienda_queda_fuera(self):
        """Un piso no es suelo edificable: mezclarlo falsea el precio por metro."""
        candidatas = self._fuente().candidates(self._anuncio())
        assert not any("Vivienda" in c["description"] for c in candidatas)

    def test_el_precio_es_el_tipo_de_licitacion_y_no_la_fianza(self):
        primera = self._fuente().candidates(self._anuncio())[0]
        assert primera["price_eur"] == 468000.0
        assert primera["raw"]["price_label"] == "tipo de licitacion"

    def test_las_hectareas_se_convierten_a_metros(self):
        """Confundirlas cambia el precio por metro en dos órdenes de magnitud."""
        rustica = self._fuente().candidates(self._anuncio())[1]
        assert rustica["area_m2"] == 35_000.0
        assert rustica["price_eur_m2"] == 2.5

    def test_saca_municipio_provincia_y_referencia_catastral(self):
        primera = self._fuente().candidates(self._anuncio())[0]
        assert primera["municipality_name"] == "León"
        assert primera["province"] == "León"
        assert primera["cadastral_ref"] == "9872023VH5797S0001WX"

    def test_el_identificador_externo_distingue_los_lotes(self):
        """Sin el lote en la clave, veinticinco fincas se pisaban entre sí."""
        externos = [c["external_id"] for c in self._fuente().candidates(self._anuncio())]
        assert externos == ["BOE-B-2026-23129-1", "BOE-B-2026-23129-3"]

    def test_sin_precio_o_sin_superficie_no_hay_candidata(self):
        """El criterio de la app es el precio por metro: inventarlo sería peor."""
        anuncio = {"identificador": "BOE-B-2026-1",
                   "titulo": "Anuncio de subasta de solar",
                   "texto": "Solar en Soria. Tipo de licitación: 40.000,00 euros."}
        assert self._fuente().candidates(anuncio) == []


class TestFiltrosDelAnuncio:
    """Las reglas que deciden si un anuncio interesa, una a una."""

    def test_reconoce_la_venta_aunque_no_diga_subasta(self):
        """ADIF y SEPES enajenan por pliego: «subasta» sola los dejaba fuera."""
        from app.sources.boe_anuncios import is_sale

        assert is_sale("Anuncio de enajenación de parcelas sobrantes")
        assert is_sale("Venta de solar del patrimonio municipal")
        assert not is_sale("Edicto de notificación de sentencia")

    def test_un_garaje_no_pasa_por_nombrar_su_parcela_catastral(self):
        """«Parcela catastral» sale en el anuncio de cualquier inmueble."""
        from app.sources.boe_anuncios import is_land

        assert not is_land("Plaza de garaje. Referencia catastral de la parcela 1234")
        assert is_land("Solar sin edificar de 300 m2")

    def test_la_superficie_que_vale_es_la_primera_del_texto(self):
        """Un lindero de tres hectáreas no es la finca que se vende."""
        from app.sources.boe_anuncios import parse_area

        assert parse_area("solar de 800 m2 lindante con monte de 3 hectáreas") == 800.0

    def test_el_precio_no_es_la_deuda_reclamada(self):
        """Los edictos citan la deuda y las costas antes que el tipo."""
        from app.sources.boe_anuncios import parse_price

        texto = ("Cantidad reclamada: 900.000,00 euros. "
                 "Tipo de subasta: 125.400,00 euros.")
        assert parse_price(texto) == (125400.0, "tipo de subasta")

    def test_gana_la_provincia_que_se_nombra_antes(self):
        """El anuncio cita la finca primero y el juzgado que la subasta después."""
        from app.sources.boe_anuncios import parse_province

        assert parse_province(
            "Finca en Cuenca, por el Juzgado de Primera Instancia de Madrid"
        ) == "Cuenca"

    def test_sin_lotes_numerados_el_anuncio_va_entero(self):
        """Las subastas judiciales son de una sola finca y no numeran nada."""
        from app.sources.boe_anuncios import split_lots

        assert split_lots("Solar único en Ávila de 500 m2") == [
            ("", "Solar único en Ávila de 500 m2")
        ]

    def test_filtra_por_provincia_con_lo_que_dice_el_anuncio(self):
        """El Boletín es nacional: no hay forma de pedirle sólo una provincia."""
        from app.sources.boe_anuncios import BoeAnunciosSource

        anuncios = [{
            "identificador": "BOE-B-2026-7",
            "titulo": "Anuncio de subasta de solar",
            "texto": ("Solar sito en el término municipal de Reinosa, Cantabria, "
                      "de 1.000 m2. Tipo de licitación: 50.000,00 euros."),
        }]
        fuente = BoeAnunciosSource()
        assert fuente.from_crawl(anuncios, province="Cantabria")["candidates"]
        fuera = fuente.from_crawl(anuncios, province="Sevilla")
        assert fuera["candidates"] == []
        assert fuera["discarded"]["otra_provincia"] == 1

    def test_aparece_en_el_registro_de_fuentes(self):
        from app.sources.registry import build_sources

        assert "boe_anuncios" in {s.key for s in build_sources()}


class TestPorQueNoSalenIdentificadores:
    """«0 identificadas» no decía si fallaba la red, el filtro o no había nada."""

    SUMARIO = {"item": [
        {"identificador": "BOE-B-2026-1",
         "titulo": "Anuncio de subasta de finca rústica",
         "url_xml": "/diario_boe/xml.php?id=BOE-B-2026-1"},
        {"identificador": "BOE-B-2026-2",
         "titulo": "Anuncio de enajenación de parcela municipal",
         "url_xml": "/diario_boe/xml.php?id=BOE-B-2026-2"},
    ]}

    def _con_respuestas(self, monkeypatch, cuerpo, status=200):
        import httpx

        def responder(self, method, url, **kw):
            if "sumario" in str(url):
                return httpx.Response(
                    200,
                    json={"data": {"sumario": TestPorQueNoSalenIdentificadores.SUMARIO}},
                    request=httpx.Request(method, url),
                )
            return httpx.Response(status, text=cuerpo,
                                  request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", responder)

    def test_separa_los_fallos_de_descarga_de_los_anuncios_sin_subasta(
        self, monkeypatch
    ):
        from datetime import date

        from app.sources.boe_api import BoeSumarioSource

        self._con_respuestas(monkeypatch, "<documento>Sin identificador.</documento>")
        dia = BoeSumarioSource().crawl(days=1, today=date(2026, 9, 8))["days"][0]
        assert dia["auction_announcements"] == 2
        assert dia["downloaded"] == 2
        assert dia["download_errors"] == 0
        assert dia["without_auction_id"] == 2
        assert dia["with_auction_id"] == 0

    def test_un_anuncio_que_no_se_descarga_se_cuenta_como_fallo(self, monkeypatch):
        from datetime import date

        from app.sources.boe_api import BoeSumarioSource

        self._con_respuestas(monkeypatch, "", status=503)
        dia = BoeSumarioSource().crawl(days=1, today=date(2026, 9, 8))["days"][0]
        assert dia["download_errors"] == 2
        assert dia["downloaded"] == 0
        assert "last_error" in dia

    def test_el_recorrido_guarda_el_xml_para_no_pedirlo_dos_veces(self, monkeypatch):
        """Dos fuentes salen del mismo paseo: pedirlo dos veces lo hacía cortar."""
        from datetime import date

        from app.sources.boe_api import BoeSumarioSource

        self._con_respuestas(monkeypatch, "<documento><texto>Solar</texto></documento>")
        recorrido = BoeSumarioSource().crawl(days=1, today=date(2026, 9, 8))
        assert all("xml" in a for a in recorrido["announcements"])

    def test_el_sumario_ve_ahora_las_enajenaciones(self):
        """Las ventas por pliego no dicen «subasta» en el título."""
        from app.sources.boe_api import BoeSumarioSource

        anuncios = BoeSumarioSource().auction_items(self.SUMARIO)
        assert len(anuncios) == 2


class TestVendedoresPublicosDeSuelo:
    """SEPES, ADIF, INVIED y Patrimonio: catalogados, no raspados."""

    def test_estan_en_el_registro(self):
        from app.sources.registry import build_sources

        claves = {s.key for s in build_sources()}
        assert {"sepes", "adif_suelos", "invied", "patrimonio_estado"} <= claves

    def test_no_se_cuentan_como_fuente_de_anuncios(self):
        """Su web responde, pero de ahí no sale ninguna oferta consultable:
        marcarlas como «listings» haría que el panel prometiera lo que no hay."""
        from app.sources.suelo_publico import iter_public_land_sources

        assert all(s.kind == "catalog" for s in iter_public_land_sources())

    def test_dicen_por_donde_se_leen_de_verdad(self, monkeypatch):
        import httpx

        from app.sources.suelo_publico import SepesSource

        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                200, text="", request=httpx.Request(method, url)),
        )
        estado = SepesSource().check()
        assert estado.ok
        assert "BOE" in estado.detail
        assert estado.extra["discovered_via"] == "boe_anuncios"


class TestEdictoJudicial:
    """El formato del grueso de los anuncios: una finca, descrita en prosa."""

    TEXTO = (
        "Edicto. La Letrada de la Administración de Justicia del Juzgado de "
        "Primera Instancia n.º 3 de Santander hago saber: que en la ejecución "
        "hipotecaria 123/2024 se ha acordado sacar a pública subasta el bien que "
        "se dirá, en el Portal de Subastas con identificador SUB-JA-2026-123456. "
        "Cantidad reclamada: 187.432,11 euros. Descripción: Rústica. Terreno en "
        "el término municipal de Piélagos, de superficie {superficie}. "
        "Referencia catastral 39052A005001230000XY. Valor de tasación a efectos "
        "de subasta: 96.000,00 euros."
    )

    def _candidatas(self, superficie):
        from app.sources.boe_anuncios import BoeAnunciosSource

        anuncio = {"identificador": "BOE-B-2026-9",
                   "titulo": "Anuncio de subasta judicial",
                   "texto": self.TEXTO.format(superficie=superficie)}
        return BoeAnunciosSource().candidates(anuncio)

    def test_lee_el_edicto_con_la_superficie_en_cifras(self):
        candidata = self._candidatas("1.200 metros cuadrados")[0]
        assert candidata["area_m2"] == 1200.0
        assert candidata["municipality_name"] == "Piélagos"
        assert candidata["province"] == "Cantabria"

    def test_lee_la_superficie_escrita_en_letra(self):
        """El Registro la escribe así tan a menudo como en cifras, y sin esto
        se perdía entera justo la mitad de los edictos judiciales."""
        assert self._candidatas("mil doscientos metros cuadrados")[0]["area_m2"] == 1200.0

    def test_el_precio_es_la_tasacion_y_no_la_deuda(self):
        assert self._candidatas("1.200 metros cuadrados")[0]["price_eur"] == 96000.0


class TestNumerosEnLetra:
    """Sólo lo que hace falta para una superficie: cardinales hasta millones."""

    def test_compone_las_decenas_y_los_millares(self):
        from app.sources.boe_anuncios import words_to_number

        assert words_to_number("mil doscientos") == 1200.0
        assert words_to_number("novecientos cincuenta") == 950.0
        assert words_to_number("dos mil quinientos") == 2500.0
        assert words_to_number("cuarenta y cinco") == 45.0

    def test_una_palabra_desconocida_no_da_un_numero_a_medias(self):
        """Un número incompleto falsearía el precio por metro sin avisar."""
        from app.sources.boe_anuncios import words_to_number

        assert words_to_number("la finca sita") is None
        assert words_to_number("") is None

    def test_las_cifras_mandan_sobre_las_letras(self):
        from app.sources.boe_anuncios import parse_area

        assert parse_area("de 640 m2, o sea seiscientos cuarenta") == 640.0


class TestSinDuplicarLoDelPortal:
    """Un edicto judicial lleva SUB- y además describe la finca en su texto."""

    ANUNCIO = {
        "identificador": "BOE-B-2026-9",
        "titulo": "Anuncio de subasta judicial",
        "auction_ids": ["SUB-JA-2026-123456"],
        "texto": ("Subasta con identificador SUB-JA-2026-123456. Terreno en el "
                  "término municipal de Piélagos de 1.200 metros cuadrados. "
                  "Valor de tasación: 96.000,00 euros."),
    }

    def _fuente(self):
        from app.sources.boe_anuncios import BoeAnunciosSource

        return BoeAnunciosSource()

    def test_si_el_portal_ya_la_trajo_no_se_repite(self):
        """Saldría la misma parcela dos veces, con dos claves que no se cruzan."""
        salida = self._fuente().from_crawl(
            [self.ANUNCIO], ya_cubiertos=frozenset({"SUB-JA-2026-123456"})
        )
        assert salida["candidates"] == []
        assert salida["discarded"]["ya_estaba_en_el_portal"] == 1

    def test_si_el_portal_no_contesto_se_recoge_igual(self):
        """Es cuando más falta hace: sin el portal, el texto es lo único que hay."""
        salida = self._fuente().from_crawl([self.ANUNCIO])
        assert len(salida["candidates"]) == 1

    def test_el_portal_apunta_lo_que_de_verdad_convirtio(self, monkeypatch):
        """Sólo cuentan las que llegaron a candidata, no las que se intentaron."""
        import app.discovery as discovery
        from app.sources.boe import BoeSubastasSource

        detalle = {"id_sub": "SUB-JA-2026-123456",
                   "url": "https://subastas.boe.es/x", "raw_fields": {},
                   "asset_type": "solar", "description": "solar de 1.200 m2",
                   "minimum_bid": "96.000,00 €", "area": "1.200 m2"}
        monkeypatch.setattr(BoeSubastasSource, "detail", lambda self, i: detalle)

        candidatas: list[dict] = []
        info = discovery._from_boe_api(
            None, 10, candidatas,
            {"ids": ["SUB-JA-2026-123456"], "days": [], "announcements": []},
        )
        assert info["covered_auctions"] == ["SUB-JA-2026-123456"]


class TestHostEquivocadoEnRapidApi:
    """Un 404 en las rutas de búsqueda casi nunca es «no hay datos».

    En RapidAPI varios revendedores sirven el mismo portal con rutas distintas,
    y los nombres se parecen tanto —fotocasa1 y fotocasa3, idealista17 y
    idealista-api1— que apuntar al que no es resulta facilísimo. El panel decía
    «HTTP 404» a secas y obligaba a adivinar justo eso.
    """

    def _sondear(self, monkeypatch, host=None, respuesta=None):
        import httpx

        from app.config import get_settings
        import app.sources.rapidapi as rapidapi

        monkeypatch.setenv("RAPIDAPI_KEYS", "rapidapi_idealista=clave")
        if host:
            monkeypatch.setenv("RAPIDAPI_HOSTS", f"rapidapi_idealista={host}")
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        monkeypatch.setattr(
            httpx.Client, "request",
            respuesta or (lambda self, method, url, **kw: httpx.Response(
                404, text="", request=httpx.Request(method, url))),
        )
        fuente = next(s for s in rapidapi.iter_rapidapi_sources()
                      if s.key == "rapidapi_idealista")
        estado = fuente.check()
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        return estado

    def test_el_404_dice_que_el_host_no_reconoce_las_rutas(self, monkeypatch):
        estado = self._sondear(monkeypatch, "otro-proveedor.p.rapidapi.com")
        assert "no reconoce las rutas" in estado.detail

    def test_nombra_los_dos_hosts_para_poder_compararlos(self, monkeypatch):
        """Sin ver ambos no se nota que son proveedores distintos."""
        estado = self._sondear(monkeypatch, "otro-proveedor.p.rapidapi.com")
        assert "otro-proveedor.p.rapidapi.com" in estado.detail
        assert "idealista-api1.p.rapidapi.com" in estado.detail
        assert estado.extra["host_sobrescrito"] is True

    def test_el_host_en_uso_sale_siempre_en_el_estado(self, monkeypatch):
        """Es el dato que faltaba para diagnosticar sin tocar el servidor."""
        estado = self._sondear(monkeypatch, "otro-proveedor.p.rapidapi.com")
        assert estado.extra["host"] == "otro-proveedor.p.rapidapi.com"
        assert estado.extra["host_por_defecto"] == "idealista-api1.p.rapidapi.com"


class TestRechazoDeRapidApi:
    """401 y 403 no son el mismo problema, y se trataban igual.

    El panel mandaba a suscribirse a quien ya lo estaba. Y encima descartaba el
    mensaje del propio RapidAPI, que es lo único que lo dice sin ambigüedad.
    """

    def _rechazo(self, monkeypatch, codigo, cuerpo):
        import httpx

        from app.config import get_settings
        import app.sources.rapidapi as rapidapi

        monkeypatch.setenv("RAPIDAPI_KEYS", "rapidapi_idealista=clave-de-prueba-1234")
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        monkeypatch.setattr(
            httpx.Client, "request",
            lambda self, method, url, **kw: httpx.Response(
                codigo, text=cuerpo, headers={"content-type": "application/json"},
                request=httpx.Request(method, url)),
        )
        fuente = next(s for s in rapidapi.iter_rapidapi_sources()
                      if s.key == "rapidapi_idealista")
        estado = fuente.check()
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        return estado

    def test_el_403_situa_bien_la_suscripcion_y_la_clave(self, monkeypatch):
        """La documentación de RapidAPI lo separa: la suscripción y la
        facturación son de la CUENTA, y la clave es de una aplicación. Decirlo
        al revés mandaba a mirar donde no era."""
        estado = self._rechazo(
            monkeypatch, 403, '{"message":"You are not subscribed to this API."}'
        )
        assert "suscripción es de la CUENTA" in estado.detail
        assert "cada API por separado" in estado.detail

    def test_el_401_no_afirma_cual_de_las_dos_cosas_es(self, monkeypatch):
        """Hay proveedores que devuelven 401 para «no suscrito»: afirmar que es
        la clave mandaba a rotar una clave que estaba bien."""
        estado = self._rechazo(
            monkeypatch, 401, '{"message":"Invalid API key"}'
        )
        assert "Puede ser la clave o puede ser la suscripción" in estado.detail

    def test_se_incluye_el_mensaje_del_propio_rapidapi(self, monkeypatch):
        """Es lo único que distingue los dos casos sin ambigüedad."""
        estado = self._rechazo(
            monkeypatch, 403, '{"message":"You are not subscribed to this API."}'
        )
        assert "You are not subscribed to this API." in estado.detail

    def test_la_huella_identifica_la_clave_sin_ensenarla(self, monkeypatch):
        """La causa más común de un rechazo es que el servidor no tiene la
        clave que uno cree; enseñarla entera en un panel web no es opción."""
        estado = self._rechazo(monkeypatch, 401, "{}")
        assert "clave-…1234" in estado.detail
        assert "clave-de-prueba-1234" not in estado.detail

    def test_una_ruta_fuera_del_plan_no_mata_la_fuente(self, monkeypatch):
        """«This endpoint is disabled for your subscription» dice que la
        suscripción existe y la clave vale: sólo esa ruta no entra en el plan.
        Abortar ahí daba la fuente por muerta teniendo rutas sin probar."""
        import httpx

        from app.config import get_settings
        import app.sources.rapidapi as rapidapi

        monkeypatch.setenv("RAPIDAPI_KEYS", "rapidapi_idealista17=clave")
        get_settings.cache_clear()
        rapidapi._check_cache.clear()

        probadas: list[str] = []

        def responder(self, method, url, **kw):
            probadas.append(str(url).split("?")[0].rsplit("/", 1)[-1])
            return httpx.Response(
                403, text='{"message":"This endpoint is disabled for your subscription"}',
                headers={"content-type": "application/json"},
                request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", responder)
        fuente = next(s for s in rapidapi.iter_rapidapi_sources()
                      if s.key == "rapidapi_idealista17")
        estado = fuente.check()
        get_settings.cache_clear()
        rapidapi._check_cache.clear()

        # Se prueban todas las rutas candidatas, no sólo la primera.
        assert len(probadas) == len(fuente.search_paths)
        assert "fuera del plan" in estado.detail

    def test_reconoce_las_frases_de_plan_insuficiente(self):
        import httpx

        from app.sources.rapidapi import _endpoint_fuera_del_plan

        def resp(cuerpo):
            return httpx.Response(
                403, text=cuerpo, headers={"content-type": "application/json"},
                request=httpx.Request("GET", "https://x"))

        assert _endpoint_fuera_del_plan(
            resp('{"message":"This endpoint is disabled for your subscription"}'))
        # Y no confunde el rechazo de la cuenta con el de la ruta.
        assert not _endpoint_fuera_del_plan(
            resp('{"message":"You are not subscribed to this API."}'))

    def test_un_404_en_la_ruta_de_salud_no_marca_la_fuente_en_rojo(self):
        """Pocos proveedores tienen ruta de salud. Y si la petición llegó a
        contestar 404, la pasarela la dejó pasar: la clave y la suscripción
        están bien, que es lo contrario de lo que decía el panel."""
        from app.sources.rapidapi import RapidApiFotocasaSource

        assert RapidApiFotocasaSource.health_path == "/health"


class TestPorQueNoSeIncorpora:
    """«1 descartadas por falta de datos» no permitía arreglar nada.

    Faltar el precio, no conocer el Catastro la referencia y no reconocerse el
    municipio son tres problemas distintos, con tres soluciones distintas, y se
    contaban todos en el mismo número sin decir cuál era.
    """

    def _importar(self, db, payload):
        from app.discovery import import_candidates

        return import_candidates(db, [payload])

    def test_dice_que_campo_obligatorio_falta(self, db_session):
        resultado = self._importar(db_session, {
            "source": "boe_anuncios", "external_id": "BOE-B-1",
            "area_m2": 1000.0,  # sin precio
        })
        assert resultado["skipped"] == 1
        assert "price_eur" in resultado["skipped_reasons"][0]["reason"]

    def test_dice_cuando_no_se_puede_situar_en_el_mapa(self, db_session, monkeypatch):
        import app.discovery as discovery

        monkeypatch.setattr(discovery, "_fill_from_cadastre",
                            lambda item, ref: "el WFS no devolvió geometría")
        monkeypatch.setattr(discovery, "resolve_missing_coords",
                            lambda db, item: None)
        resultado = self._importar(db_session, {
            "source": "boe_anuncios", "external_id": "BOE-B-2",
            "price_eur": 2462.0, "area_m2": 69055.0,
            "cadastral_ref": "22288C006003510000LS",
            "municipality_name": "Ballobar", "province": "Huesca",
        })
        motivo = resultado["skipped_reasons"][0]["reason"]
        assert "no se pudo situar en el mapa" in motivo
        assert "el WFS no devolvió geometría" in motivo
        assert "Ballobar" in motivo

    def test_distingue_no_tener_municipio_de_no_reconocerlo(self, db_session, monkeypatch):
        import app.discovery as discovery

        monkeypatch.setattr(discovery, "_fill_from_cadastre", lambda item, ref: "")
        monkeypatch.setattr(discovery, "resolve_missing_coords", lambda db, item: None)
        resultado = self._importar(db_session, {
            "source": "boe_anuncios", "external_id": "BOE-B-3",
            "price_eur": 1000.0, "area_m2": 500.0, "municipality_name": "",
        })
        assert "no nombra municipio" in resultado["skipped_reasons"][0]["reason"]


class TestSituarLaParcelaPorCatastro:
    """Las subastas del BOE traen fincas rústicas, y el WFS no siempre las sirve."""

    def test_el_punto_sirve_cuando_no_hay_geometria(self, monkeypatch):
        """Sin esto la candidata se tiraba entera pese a traer su referencia."""
        import app.discovery as discovery
        from app.sources.catastro import CatastroSource

        monkeypatch.setattr(CatastroSource, "parcel_geometry",
                            lambda self, ref: None)
        monkeypatch.setattr(CatastroSource, "coords_for_ref",
                            lambda self, ref: (41.5678, -0.1234))
        item: dict = {}
        assert discovery._fill_from_cadastre(item, "22056B505001480000MS") == ""
        assert (item["lat"], item["lon"]) == (41.5678, -0.1234)
        assert item["coords_precision"] == "parcel_point"

    def test_el_fallo_del_catastro_deja_de_tragarse(self, monkeypatch):
        """Una referencia mal leída y un Catastro caído se veían igual: nada."""
        import app.discovery as discovery
        from app.sources.base import SourceError
        from app.sources.catastro import CatastroSource

        def revienta(self, ref):
            raise SourceError("REFERENCIA CATASTRAL NO ENCONTRADA")

        monkeypatch.setattr(CatastroSource, "parcel_geometry", revienta)
        monkeypatch.setattr(CatastroSource, "coords_for_ref", revienta)
        motivo = discovery._fill_from_cadastre({}, "MAL")
        assert "REFERENCIA CATASTRAL NO ENCONTRADA" in motivo

    def test_lee_el_punto_de_consulta_cpmrc(self):
        from app.sources.catastro import CatastroSource

        xml = ("<consulta_coordenadas><coordenadas><coord><geo>"
               "<xcen>-0.1234</xcen><ycen>41.5678</ycen></geo>"
               "</coord></coordenadas></consulta_coordenadas>")
        assert CatastroSource.parse_coords(xml) == (41.5678, -0.1234)

    def test_un_error_del_ovc_no_se_confunde_con_parcela_inexistente(self):
        """El servicio contesta 200 también cuando falla, con el motivo dentro."""
        from app.sources.base import SourceError
        from app.sources.catastro import CatastroSource

        xml = ("<consulta_coordenadas><lerr><err>"
               "<des>REFERENCIA CATASTRAL NO ENCONTRADA</des>"
               "</err></lerr></consulta_coordenadas>")
        with pytest.raises(SourceError, match="NO ENCONTRADA"):
            CatastroSource.parse_coords(xml)


class TestMunicipioPequeno:
    """El suelo barato está en pueblos que no caben en una tabla de 116."""

    def test_si_la_tabla_no_lo_conoce_se_geocodifica(self, db_session, monkeypatch):
        """Ballobar, doscientos vecinos, no está en la tabla local: sus
        parcelas se descartaban aunque el anuncio dijera dónde estaban."""
        from app.discovery import resolve_missing_coords
        from app.sources.osm import NominatimSource

        pedido = {}

        def falso(self, query):
            pedido["query"] = query
            return {"lat": 41.6, "lon": -0.15, "display_name": "Ballobar"}

        monkeypatch.setattr(NominatimSource, "geocode", falso)
        punto = resolve_missing_coords(
            db_session, {"municipality_name": "Ballobar", "province": "Huesca"}
        )
        assert punto == (41.6, -0.15)
        # La provincia va en la consulta: hay municipios homónimos.
        assert "Huesca" in pedido["query"]

    def test_sin_municipio_no_se_inventa_un_punto(self, db_session):
        from app.discovery import resolve_missing_coords

        assert resolve_missing_coords(db_session, {"municipality_name": ""}) is None


class TestPeticionCrudaARapidApi:
    """El mecanismo para acertar con los parámetros sin redesplegar.

    Estos revendedores no documentan sus parámetros de forma fiable y cada uno
    usa los suyos: uno espera `propertyType` y el de al lado `property_type`.
    La diferencia entre acertar y no acertar es un HTTP 400 sin explicación, y
    probar a ciegas tocando código y redesplegando cuesta una tarde.
    """

    def _cliente(self, monkeypatch, responder):
        """TestClient es a su vez un cliente httpx, así que parchear
        httpx.Client interceptaría la llamada a la propia aplicación y el test
        no probaría nada. Se sustituye el método de la fuente."""
        from fastapi.testclient import TestClient

        from app.config import get_settings
        from app.main import app
        from app.sources.rapidapi import RapidApiSource

        monkeypatch.setenv("RAPIDAPI_KEY", "clave-de-prueba-1234")
        get_settings.cache_clear()
        monkeypatch.setattr(RapidApiSource, "request", responder)
        return TestClient(app)

    def test_reenvia_los_parametros_sueltos_de_la_url(self, monkeypatch):
        import httpx

        enviado: dict = {}

        def responder(self, method, url, **kw):
            enviado["params"] = kw.get("params")
            enviado["url"] = str(url)
            return httpx.Response(200, json={"success": True},
                                  request=httpx.Request(method, url))


        cliente = self._cliente(monkeypatch, responder)
        respuesta = cliente.get(
            "/api/sources/rapidapi/raw",
            params={"source": "rapidapi_idealista17", "path": "/property-search",
                    "country": "es", "property_type": "lands"},
        )
        assert respuesta.status_code == 200
        # source y path son de la herramienta; el resto va al proveedor.
        assert enviado["params"] == {"country": "es", "property_type": "lands"}
        assert enviado["url"].endswith("/property-search")

    def test_devuelve_el_cuerpo_para_poder_leer_el_error(self, monkeypatch):
        """Un HTTP 400 sin cuerpo no dice qué parámetro sobra o falta."""
        import httpx

        def responder(self, method, url, **kw):
            return httpx.Response(
                400, json={"error": "invalid_params", "message": "location required"},
                request=httpx.Request(method, url))

        cliente = self._cliente(monkeypatch, responder)
        datos = cliente.get(
            "/api/sources/rapidapi/raw",
            params={"source": "rapidapi_idealista17", "path": "/property-search"},
        ).json()
        assert datos["http_status"] == 400
        assert datos["body"]["message"] == "location required"
        assert datos["top_level_keys"] == ["error", "message"]

    def test_no_deja_elegir_el_host_ni_devuelve_la_clave(self, monkeypatch):
        """Si no, sería un puente hacia cualquier sitio con la clave de casa."""
        import httpx

        def responder(self, method, url, **kw):
            return httpx.Response(200, json={}, request=httpx.Request(method, url))

        cliente = self._cliente(monkeypatch, responder)
        datos = cliente.get(
            "/api/sources/rapidapi/raw",
            params={"source": "rapidapi_idealista17", "path": "/x",
                    "host": "evil.example.com"},
        ).json()
        assert datos["host"] == "idealista17.p.rapidapi.com"
        assert "clave-de-prueba-1234" not in str(datos)
        assert datos["key_fingerprint"] == "clave-…1234 (20 caracteres)"

    def test_una_fuente_que_no_existe_se_dice(self, monkeypatch):
        import httpx

        def responder(self, method, url, **kw):
            return httpx.Response(200, json={}, request=httpx.Request(method, url))

        cliente = self._cliente(monkeypatch, responder)
        respuesta = cliente.get(
            "/api/sources/rapidapi/raw",
            params={"source": "no_existe", "path": "/x"},
        )
        assert respuesta.status_code == 404
        assert "rapidapi_idealista17" in respuesta.json()["detail"]


class TestElMensajeDelCuatrocientos:
    """Un HTTP 400 sin su texto no permite acertar con los parámetros.

    Es el mismo fallo que ya se corrigió para el 401 y el 403 —descartar el
    cuerpo, que es donde el proveedor explica el motivo— y que aquí se había
    quedado sin corregir. Con estos revendedores es la diferencia entre leer
    «falta la ubicación» y probar nombres a ciegas.
    """

    def _sondear(self, monkeypatch, responder):
        import httpx

        from app.config import get_settings
        import app.sources.rapidapi as rapidapi

        monkeypatch.setenv("RAPIDAPI_KEY", "clave")
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        monkeypatch.setattr(httpx.Client, "request", responder)
        fuente = next(s for s in rapidapi.iter_rapidapi_sources()
                      if s.key == "rapidapi_idealista17")
        estado = fuente.check()
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        return estado

    def test_el_400_lleva_lo_que_dice_el_proveedor(self, monkeypatch):
        import httpx

        estado = self._sondear(monkeypatch, lambda self, m, u, **k: httpx.Response(
            400, json={"message": "location or locationId is required"},
            request=httpx.Request(m, u)))
        assert "location or locationId is required" in estado.detail

    def test_un_400_no_se_explica_como_host_equivocado(self, monkeypatch):
        """Un 400 dice que la ruta SÍ existe: culpar al host era falso, y
        además pisaba el mensaje que nombra el parámetro que falta."""
        import httpx

        estado = self._sondear(monkeypatch, lambda self, m, u, **k: httpx.Response(
            400, json={"message": "location is required"},
            request=httpx.Request(m, u)))
        assert "no reconoce las rutas" not in estado.detail
        assert estado.status_code == 400

    def test_si_todo_son_404_si_se_culpa_al_host(self, monkeypatch):
        """Ahí sí: el host contesta pero no conoce ninguna de las rutas."""
        import httpx

        estado = self._sondear(monkeypatch, lambda self, m, u, **k: httpx.Response(
            404, text="", request=httpx.Request(m, u)))
        assert "no reconoce las rutas" in estado.detail

    def test_lee_el_mensaje_venga_en_message_o_en_error(self):
        import httpx

        from app.sources.rapidapi import _mensaje_del_proveedor

        def resp(cuerpo):
            return httpx.Response(400, text=cuerpo,
                                  headers={"content-type": "application/json"},
                                  request=httpx.Request("GET", "https://x"))

        assert _mensaje_del_proveedor(resp('{"message":"falta algo"}')) == "falta algo"
        assert _mensaje_del_proveedor(resp('{"error":"invalid"}')) == "invalid"
        # Y si no es JSON, el texto crudo sirve igual.
        assert "Bad Request" in _mensaje_del_proveedor(resp("Bad Request"))


class TestIdealista17Verificado:
    """Parámetros y envoltorio comprobados contra el proveedor, no deducidos.

    Este conector se copió de la API oficial de Idealista y llevaba sus
    nombres: `propertyType`, `operation`, `locale`, `maxItems`, `radius` en
    metros. Este revendedor usa otros, y contestaba «Invalid request
    parameters» sin decir cuál, así que hicieron falta varias llamadas reales
    para dar con ellos. Se fijan aquí para que no se pierdan.
    """

    # Respuesta real de /property-search-by-coordinates, recortada.
    REAL = {
        "success": True,
        "data": {
            "total": 1571, "totalPages": 53, "currentPage": 1, "itemsPerPage": 30,
            "listings": [{
                "propertyCode": "109772825", "price": 295000,
                "propertyType": "flat", "operation": "sale", "size": 78,
                "rooms": 2, "bathrooms": 1,
                "address": "Flat in Calle de San Hermenegildo, Madrid",
                "province": "Madrid", "municipality": "Madrid",
                "latitude": 40.4262, "longitude": -3.7053,
                "url": "https://www.idealista.com/inmueble/109772825/",
            }],
        },
    }

    def _fuente(self):
        from app.sources.rapidapi import RapidApiIdealista17Source

        return RapidApiIdealista17Source()

    def test_los_nombres_son_los_que_admite_el_proveedor(self):
        params = self._fuente().build_params(40.4168, -3.7038, 3.0)
        assert params["latitude"] == 40.4168
        assert params["longitude"] == -3.7038
        assert params["radius_km"] == 3
        assert params["language"] == "en"
        assert params["search_type"] == "for_sale"
        assert params["sort_order"] == "default"

    def test_el_radio_va_en_kilometros(self):
        """En metros -como la API oficial- la petición se rechaza."""
        assert self._fuente().build_params(40.0, -3.0, 15.0)["radius_km"] == 15
        # Nunca cero: un radio de 400 m redondearía a 0 y no busca nada.
        assert self._fuente().build_params(40.0, -3.0, 0.4)["radius_km"] == 1

    def test_lee_los_anuncios_del_envoltorio_real(self):
        """Vienen en data.listings, no en elementList como la API oficial."""
        from app.sources.rapidapi import extract_listings

        assert len(extract_listings(self.REAL)) == 1

    def test_normaliza_un_anuncio_real(self):
        from app.sources.rapidapi import extract_listings

        anuncio = self._fuente().normalize(extract_listings(self.REAL)[0])
        assert anuncio["external_id"] == "109772825"
        assert anuncio["price_eur"] == 295000.0
        assert anuncio["area_m2"] == 78.0
        assert (anuncio["lat"], anuncio["lon"]) == (40.4262, -3.7053)
        assert anuncio["municipality_name"] == "Madrid"

    def test_saca_el_identificador_de_zona_de_smart_search(self):
        """/property-search no busca por coordenadas: quiere el locationId."""
        respuesta = {"success": True, "data": {"searchText": "chamberi", "results": [
            {"name": "Chamberí, Madrid", "type": "location",
             "locationId": "0-EU-ES-28-07-001-079-04"}]}}
        assert self._fuente().parse_location_id(respuesta) == "0-EU-ES-28-07-001-079-04"

    def test_sin_sugerencias_no_se_inventa_una_zona(self):
        assert self._fuente().parse_location_id({"data": {"results": []}}) == ""

    def test_la_zona_se_manda_como_location_ids(self):
        params = self._fuente().build_params(
            40.0, -3.0, 5.0, location_ids="0-EU-ES-28-07-001-079")
        assert params["location_ids"] == "0-EU-ES-28-07-001-079"

    def test_el_tipo_de_inmueble_se_puede_cambiar_sin_tocar_codigo(self, monkeypatch):
        """`lands` es lo que necesita la app; `homes` es lo comprobado. Si este
        revendedor lo nombrara de otra forma, no debe costar un despliegue."""
        from app.config import get_settings

        assert self._fuente().property_type == "lands"
        monkeypatch.setenv("RAPIDAPI_PARAMS",
                           "rapidapi_idealista17.property_type=homes")
        get_settings.cache_clear()
        assert self._fuente().property_type == "homes"
        get_settings.cache_clear()


class TestQueSeEnvioDeVerdad:
    """El proveedor se queja de parámetros concretos; hay que ver los enviados.

    Fotocasa contestó «Parameters minPrice, maxPrice, conservationStatus,
    features are not allowed for property type LAND». Sin saber cuáles se
    mandaron no se puede cruzar esa lista con nada, y el conector sólo manda
    algunos de ellos y sólo a veces.
    """

    def _sondear(self, monkeypatch, responder, fuente="rapidapi_fotocasa"):
        import httpx

        from app.config import get_settings
        import app.sources.rapidapi as rapidapi

        monkeypatch.setenv("RAPIDAPI_KEY", "clave-de-prueba-1234")
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        monkeypatch.setattr(httpx.Client, "request", responder)
        origen = next(s for s in rapidapi.iter_rapidapi_sources() if s.key == fuente)
        estado = origen.check()
        get_settings.cache_clear()
        rapidapi._check_cache.clear()
        return estado

    def test_el_400_dice_que_parametros_se_enviaron(self, monkeypatch):
        import httpx

        def responder(self, m, u, **k):
            # Como en producción: no tiene ruta de salud, y la búsqueda es la
            # que se queja de los parámetros.
            if "/health" in str(u):
                return httpx.Response(404, text="", request=httpx.Request(m, u))
            return httpx.Response(
                400, json={"message": "Parameters minPrice, maxPrice are not "
                                      "allowed for property type LAND"},
                request=httpx.Request(m, u))

        # Fotocasa resuelve antes la zona con Nominatim; aquí no toca red.
        from app.sources.rapidapi import RapidApiFotocasaSource

        monkeypatch.setattr(RapidApiFotocasaSource, "resolve_location",
                            lambda self, lat, lon, query=None: "724,1")
        estado = self._sondear(monkeypatch, responder)
        assert "not allowed for property type LAND" in estado.detail
        assert "enviados:" in estado.detail
        assert "propertyType" in estado.detail

    def test_el_rechazo_por_plan_lleva_la_huella_de_la_clave(self, monkeypatch):
        """El mismo endpoint responde distinto con dos claves distintas; sin
        verla no se nota que el servidor usa una y tú pruebas con otra."""
        import httpx

        estado = self._sondear(
            monkeypatch,
            lambda self, m, u, **k: httpx.Response(
                401, json={"message": "This endpoint is disabled for your subscription"},
                request=httpx.Request(m, u)),
            fuente="rapidapi_idealista17")
        assert "clave-…1234" in estado.detail
        assert "fuera del plan" in estado.detail
