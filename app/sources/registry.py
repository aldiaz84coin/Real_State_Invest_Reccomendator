"""Registro unico de fuentes y comprobacion de acceso en paralelo."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.models import SourceCheck
from app.sources.base import BaseSource, SourceStatus
from app.sources.boe import BoeSubastasSource
from app.sources.catastro import CatastroSource
from app.sources.idealista import FotocasaSource, IdealistaSource, PisosComSource
from app.sources.osm import NominatimSource, OverpassSource
from app.sources.prices import IneSource, MivauLandPriceSource
from app.sources.rapidapi import iter_rapidapi_sources
from app.sources.rental import AirDnaSource, AirRoiSource, InsideAirbnbSource


def build_sources() -> list[BaseSource]:
    """Todas las fuentes que la app conoce, usadas o descartadas.

    Las descartadas (Fotocasa, pisos.com, AirDNA) se incluyen a proposito:
    el panel debe explicar por que no se consultan, no ocultarlo.
    """
    return [
        IdealistaSource(),
        FotocasaSource(),
        PisosComSource(),
        BoeSubastasSource(),
        *iter_rapidapi_sources(),
        CatastroSource(),
        IneSource(),
        MivauLandPriceSource(),
        OverpassSource(),
        NominatimSource(),
        InsideAirbnbSource(),
        AirRoiSource(),
        AirDnaSource(),
    ]


def get_source(key: str) -> BaseSource | None:
    for source in build_sources():
        if source.key == key:
            return source
    return None


def check_all(db: Session | None = None, timeout: float = 45.0) -> dict[str, Any]:
    """Comprueba todas las fuentes a la vez y resume el estado.

    Se ejecuta en paralelo porque son peticiones de red independientes; en
    serie tardaria mas que el timeout razonable de una peticion web.
    """
    sources = build_sources()

    def _safe_check(source: BaseSource) -> SourceStatus:
        try:
            return source.check()
        except Exception as exc:  # una fuente rota no debe tumbar el panel
            return source._status("error", f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        statuses = list(pool.map(_safe_check, sources))

    if db is not None:
        for status in statuses:
            db.add(
                SourceCheck(
                    source=status.key,
                    ok=status.ok,
                    status_code=status.status_code,
                    latency_ms=status.latency_ms,
                    detail=status.detail[:2000],
                )
            )
        db.commit()

    required_ok = all(s.ok for s in statuses if s.required)
    has_listings = any(s.ok for s in statuses if s.kind == "listings")
    official_listings = any(s.ok for s in statuses if s.key in ("idealista", "boe_subastas"))
    fallback_listings = any(s.ok for s in statuses if s.key.startswith("rapidapi_"))

    return {
        "sources": [s.as_dict() for s in statuses],
        "summary": {
            "total": len(statuses),
            "ok": sum(1 for s in statuses if s.ok),
            "needs_credentials": sum(1 for s in statuses if s.access == "needs_credentials"),
            "unavailable": sum(1 for s in statuses if s.access == "unavailable"),
            "error": sum(1 for s in statuses if s.access == "error"),
            "required_ok": required_ok,
            "listings_available": has_listings,
            "official_listings": official_listings,
            "fallback_listings": fallback_listings,
            "operational": required_ok,
            "note": (
                "Las fuentes obligatorias (Catastro, INE, OSM) son abiertas y no "
                "necesitan clave. Las Subastas del BOE aportan ofertas activas de "
                "inmuebles en toda Espana sin clave ni coste; Idealista anade el "
                "mercado libre y requiere clave propia."
            ),
        },
    }
