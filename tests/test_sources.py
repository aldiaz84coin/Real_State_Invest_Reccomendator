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
