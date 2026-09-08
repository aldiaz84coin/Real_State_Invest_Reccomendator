"""Infraestructura comun a todos los conectores de fuentes externas."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.config import get_settings

# Fallos que casi siempre son pasajeros y merecen otro intento. El primero es
# el habitual con los servicios OVC del Catastro: una pila .NET antigua que
# corta la conexion sin responder cuando esta cargada.
TRANSIENT_EXCEPTIONS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)
# 429 se incluye porque varias fuentes publicas lo usan como freno momentaneo.
TRANSIENT_STATUS = (429, 500, 502, 503, 504)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.6


@dataclass
class SourceStatus:
    """Resultado de comprobar el acceso a una fuente.

    `access` distingue tres cosas que suelen confundirse:
      - "ok": la fuente responde y se puede usar ya.
      - "needs_credentials": la fuente existe y es legal, pero falta la clave.
      - "unavailable": no hay via publica de lectura, o la red la bloquea.
    """

    key: str
    name: str
    kind: str                     # listings | cadastre | prices | geo | rental
    access: str                   # ok | needs_credentials | unavailable | error
    required: bool
    detail: str = ""
    status_code: int | None = None
    latency_ms: int | None = None
    docs_url: str = ""
    licence: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.access == "ok"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


class SourceError(RuntimeError):
    """Fallo recuperable al hablar con una fuente externa."""


class SourceBlocked(SourceError):
    """La política de red del entorno impide llegar a la fuente.

    Es SourceError para que cualquier código que ya atrapa esa excepción lo
    cubra: dejarlo escapar como httpx.ProxyError provocaba errores 500 en los
    endpoints, y era un fallo que se repetía en cada conector nuevo.
    """


class BaseSource:
    """Conector. Cada fuente implementa `check()` y sus metodos de consulta."""

    key: str = "base"
    name: str = "Base"
    kind: str = "geo"
    required: bool = False
    docs_url: str = ""
    licence: str = ""

    def __init__(self) -> None:
        self.settings = get_settings()

    def client(self, **kwargs: Any) -> httpx.Client:
        return httpx.Client(
            timeout=self.settings.http_timeout,
            headers=self.settings.http_headers,
            follow_redirects=True,
            **kwargs,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF_SECONDS,
        **kwargs: Any,
    ) -> httpx.Response:
        """Peticion HTTP que reintenta los fallos pasajeros.

        Un corte de conexion o un 503 puntual no significan que la fuente no
        sirva: sin reintentos, una caida de un segundo del Catastro se
        reportaba como fuente caida. Los errores permanentes (404, 401, un
        bloqueo del proxy) se propagan al primer intento, porque reintentarlos
        solo suma latencia.
        """
        last_exception: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                with self.client() as client:
                    response = client.request(method, url, **kwargs)
            except httpx.ProxyError as exc:
                # Reintentar una denegación de política no sirve de nada.
                raise SourceBlocked(
                    f"La red de este entorno bloquea el acceso a {url}: {exc}"
                ) from None
            except TRANSIENT_EXCEPTIONS as exc:
                last_exception = exc
                if attempt == attempts:
                    raise
            else:
                if response.status_code in TRANSIENT_STATUS and attempt < attempts:
                    last_exception = None
                else:
                    return response

            # Espera creciente para no insistir sobre un servicio saturado.
            time.sleep(backoff * (2 ** (attempt - 1)))

        if last_exception is not None:  # pragma: no cover - defensivo
            raise last_exception
        raise SourceError(f"Sin respuesta utilizable de {url} tras {attempts} intentos.")

    def check(self) -> SourceStatus:  # pragma: no cover - lo implementa cada hijo
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------

    def _timed_probe(
        self,
        url: str,
        *,
        method: str = "GET",
        expect: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> SourceStatus:
        """Lanza una peticion real y traduce el resultado a SourceStatus.

        Diferencia explicitamente el bloqueo del proxy de salida (403 en el
        tunel CONNECT) de un rechazo de la propia fuente: son diagnosticos
        distintos y llevan a acciones distintas.
        """
        started = time.perf_counter()
        try:
            response = self.request(method, url, **kwargs)
        except SourceBlocked as exc:
            return self._status(
                "unavailable",
                f"Bloqueado por el proxy de salida del entorno, no por la fuente: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except TRANSIENT_EXCEPTIONS as exc:
            return self._status(
                "error",
                f"La fuente cortó la conexión en los {DEFAULT_ATTEMPTS} intentos "
                f"({type(exc).__name__}). Suele ser una caída pasajera del "
                "servicio; vuelve a comprobarlo en unos minutos.",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except httpx.HTTPError as exc:
            return self._status(
                "error",
                f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code in expect:
            return self._status("ok", "Responde correctamente.", response.status_code, latency)
        if response.status_code in (401, 403):
            return self._status(
                "needs_credentials",
                f"La fuente responde pero rechaza la peticion (HTTP {response.status_code}).",
                response.status_code,
                latency,
            )
        return self._status(
            "error", f"HTTP {response.status_code}", response.status_code, latency
        )

    def _status(
        self,
        access: str,
        detail: str = "",
        status_code: int | None = None,
        latency_ms: int | None = None,
        **extra: Any,
    ) -> SourceStatus:
        return SourceStatus(
            key=self.key,
            name=self.name,
            kind=self.kind,
            access=access,
            required=self.required,
            detail=detail,
            status_code=status_code,
            latency_ms=latency_ms,
            docs_url=self.docs_url,
            licence=self.licence,
            extra=extra,
        )
