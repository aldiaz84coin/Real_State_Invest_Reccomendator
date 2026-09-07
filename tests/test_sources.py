"""Tests de la capa de conectores: reintentos y clasificación de estados.

El fallo que motivó estos tests es real: el servicio OVC del Catastro cerró la
conexión sin responder y la app lo reportó como fuente caída. Aquí se reproduce
ese corte con un servidor local que se comporta igual.
"""
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
        """Una denegación de política de red no se arregla insistiendo."""
        calls = {"n": 0}

        def fake_request(self, method, url, **kwargs):
            calls["n"] += 1
            raise httpx.ProxyError("403 Forbidden")

        monkeypatch.setattr(httpx.Client, "request", fake_request)
        with pytest.raises(httpx.ProxyError):
            source.request("GET", "http://ejemplo.invalido/")
        assert calls["n"] == 1


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
        params = self._source().build_params(36.72, -4.42, 15.0, page=2, max_price=90000)
        assert params["operation"] == "sale"
        assert params["propertyType"] == "lands"
        assert params["country"] == "es"
        assert params["radius"] == 15000
        assert params["page"] == 2
        assert params["maxPrice"] == 90000

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
                code = 404 if "by-coordinates" in url else 200
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
            with pytest.raises(SourceError, match="rechazada"):
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

    def test_el_401_se_explica_como_falta_de_suscripcion(self, monkeypatch):
        """Es la causa real: en RapidAPI hay que suscribirse a cada API."""
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
            assert "suscribirse a CADA API" in status.detail
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
                    json={"suggestions": [
                        {"name": "Madrid",
                         "combinedLocations": "724,14,28,173,0,28079,0,0,0"}
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
                    200, json={"combinedLocations": "1,2,3"},
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
                    200, json={"suggestions": [{"nombre": "Madrid", "zoneId": 28079}]},
                    request=httpx.Request(method, url),
                ),
            )
            with pytest.raises(SourceError) as error:
                source.resolve_location(40.4, -3.7, query="madrid")
            mensaje = str(error.value)
            assert "identificador de zona" in mensaje
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
            for clave in ("combinedLocations", "combinedLocation", "locationIds"):
                source._locations_cache.clear()
                monkeypatch.setattr(
                    httpx.Client, "request",
                    lambda self, method, url, _k=clave, **kw: httpx.Response(
                        200, json={"data": [{_k: "1,2,3"}]},
                        request=httpx.Request(method, url),
                    ),
                )
                assert source.resolve_location(40.4, -3.7, query="madrid") == "1,2,3", clave
        finally:
            get_settings.cache_clear()

    def test_suggestions_raw_devuelve_el_cuerpo_entero(self, monkeypatch):
        from app.config import get_settings

        source = self._source(monkeypatch)
        try:
            monkeypatch.setattr(
                httpx.Client, "request",
                lambda self, method, url, **kw: httpx.Response(
                    200, json={"suggestions": [{"zoneId": 28079}]},
                    request=httpx.Request(method, url),
                ),
            )
            raw = source.suggestions_raw("madrid")
            assert raw["http_status"] == 200
            assert raw["body"] == {"suggestions": [{"zoneId": 28079}]}
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
                    200, json={"combinedLocations": "ZONA-MARBELLA"},
                    request=httpx.Request(method, url),
                )

            monkeypatch.setattr(httpx.Client, "request", fake)
            params = source.prepare_params(36.51, -4.88, 15, page=1)
            assert params["combinedLocations"] == "ZONA-MARBELLA"
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
                    200, json={"combinedLocations": "724,14,29,0,0,29067,0,0,0"},
                    request=httpx.Request(method, url),
                ),
            )
            params = source.prepare_params(36.7213, -4.4214, 15.0, page=1)
            # Es el parámetro que su API declara obligatorio.
            assert params["combinedLocations"] == "724,14,29,0,0,29067,0,0,0"
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
