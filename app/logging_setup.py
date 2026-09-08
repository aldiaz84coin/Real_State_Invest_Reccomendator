"""Registro de actividad de la aplicacion.

Los logs por defecto de uvicorn dicen que llego una peticion y con que codigo,
pero no cuanto tardo, ni con que parametros, ni que paso dentro. Cuando algo
falla en produccion eso obliga a adivinar. Aqui se anaden las tres cosas y se
deja el nivel configurable por entorno.
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("investment")

# Rutas que se consultan constantemente y no aportan nada al depurar.
QUIET_PATHS = {"/health", "/favicon.ico"}


def configure_logging(level: str = "INFO") -> None:
    """Deja un formato con hora, nivel y origen, legible en el log de Fly."""
    numeric = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric)

    logging.getLogger("investment").setLevel(numeric)
    # El log de acceso de uvicorn duplicaria lo que ya registra el middleware.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").setLevel(numeric)
    # httpx cuenta cada peticion saliente, util para ver que fuente falla.
    logging.getLogger("httpx").setLevel(
        logging.INFO if numeric <= logging.DEBUG else logging.WARNING
    )


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Registra cada peticion con su duracion, y los fallos con su traza.

    Un 500 con la traza completa y los parametros que lo provocaron se depura
    en un minuto; sin ellos hay que reproducirlo a ciegas.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in QUIET_PATHS:
            return await call_next(request)

        request_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        detalle = _describe(request)

        logger.info("→ %s %s %s%s", request_id, request.method, request.url.path, detalle)
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            logger.exception(
                "✗ %s %s %s falló tras %.0f ms%s",
                request_id, request.method, request.url.path, elapsed, detalle,
            )
            raise

        elapsed = (time.perf_counter() - started) * 1000
        nivel = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            nivel, "← %s %s %s %d en %.0f ms",
            request_id, request.method, request.url.path, response.status_code, elapsed,
        )
        return response


def _describe(request: Request) -> str:
    """Parametros de la peticion, recortados y sin secretos."""
    params = dict(request.query_params)
    for clave in list(params):
        if any(s in clave.lower() for s in ("key", "secret", "token", "password")):
            params[clave] = "***"
    if not params:
        return ""
    texto = ", ".join(f"{k}={v}" for k, v in params.items() if v != "")
    return f" [{texto[:300]}]" if texto else ""


def log_operation(name: str, **fields: Any) -> None:
    """Traza de una operacion de negocio: ingesta, simulacion, consulta a fuente."""
    detalle = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("· %s %s", name, detalle)
