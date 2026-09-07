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

    @pytest.mark.parametrize("falta", ["price", "size", "latitude", "propertyCode"])
    def test_descarta_anuncios_sin_datos_imprescindibles(self, falta):
        """Sin precio, superficie o coordenadas el anuncio no sirve para analizar."""
        item = {
            "propertyCode": "1", "price": 1000, "size": 500,
            "latitude": 36.7, "longitude": -4.4,
        }
        del item[falta]
        assert self._source().normalize(item) is None

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
