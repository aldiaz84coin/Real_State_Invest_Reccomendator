"""Configuración común de los tests.

La regla que fija este fichero: **ningún test sale a la red**. Un test que
llama de verdad a una fuente externa no prueba el código, prueba la conexión;
y como este entorno de desarrollo bloquea la salida, esos tests pasaban aquí
por el camino del error y se comportaban distinto en CI, donde la red sí
funciona. Justamente así se coló un fallo de integración.

Quien necesite simular una respuesta HTTP debe parchear el método concreto de
la fuente (`geocode`, `search_attempts`...) o `httpx.Client.request`, que es lo
que ya hacen los tests existentes.
"""
import httpx
import pytest


class SalidaDeRedProhibida(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def sin_red(monkeypatch, request):
    """Corta cualquier conexión real que no se haya simulado.

    Se salta con la marca `red_real`, que hoy no usa ningún test y que hay que
    justificar antes de añadir.
    """
    if request.node.get_closest_marker("red_real"):
        return

    original = httpx.HTTPTransport.handle_request

    def prohibido(self, peticion, *args, **kwargs):
        # Un servidor de mentira levantado por el propio test en localhost si
        # vale: es codigo controlado, no una fuente externa.
        if peticion.url.host in ("127.0.0.1", "localhost", "::1"):
            return original(self, peticion, *args, **kwargs)
        raise SalidaDeRedProhibida(
            f"Un test ha intentado conectarse a {peticion.url}. Simula la "
            "respuesta en vez de salir a la red: lo que pasa aquí depende de si "
            "hay conexión, y eso hace que el test diga cosas distintas según "
            "dónde se ejecute."
        )

    # Se corta en el transporte y no en el cliente: TestClient es un cliente
    # httpx con transporte ASGI, y prohibirle el paso dejaria sin probar toda
    # la aplicacion web.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibido)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "red_real: el test necesita salida de red de verdad"
    )


@pytest.fixture
def db_session(tmp_path):
    """Base vacía y aislada para cada test.

    Va a un fichero del propio test y no a la de desarrollo: los tests que
    escriben no deben depender de lo que haya guardado antes, ni dejar rastro.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    import app.models  # noqa: F401  — registra las tablas en el metadata

    engine = create_engine(f"sqlite:///{tmp_path / 'prueba.db'}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as sesion:
        yield sesion
    engine.dispose()
