"""Descubrimiento de parcelas candidatas en las fuentes externas.

El buscador sólo consultaba la base de datos, de modo que sin una ingesta
previa por API no mostraba nada y no había forma de poblarla desde la
interfaz. Aquí se separa en dos pasos, que es como se usa de verdad:

  1. traer candidatas de las fuentes disponibles, sin guardar nada;
  2. incorporar sólo las que interesan, ya con su geometría y su puntuación.

Guardar todo lo que devuelven las fuentes llenaría la base de ruido; que el
usuario elija es lo que la mantiene útil.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import log_operation
from app.models import Listing, Municipality
from app.sources.base import SourceError
from app.sources.boe import BoeSubastasSource
from app.sources.boe_anuncios import BoeAnunciosSource
from app.sources.boe_api import BoeSumarioSource
from app.sources.idealista import IdealistaSource
from app.sources.rapidapi import iter_rapidapi_sources

# Columnas que un anuncio puede traer. Las fuentes devuelven campos propios
# (coords_precision, source_name...) y la candidata vuelve del formulario del
# navegador: filtrar por esta lista evita tanto un Listing(**item) roto como
# que alguien inyecte columnas por el camino.
LISTING_FIELDS = frozenset({
    "source", "external_id", "url", "title", "description",
    "price_eur", "area_m2", "price_eur_m2", "lat", "lon", "address",
    "municipality_name", "province", "land_type", "buildable",
    "cadastral_ref", "parcel_geojson", "raw",
})


def discover(
    db: Session,
    *,
    lat: float | None = None,
    lon: float | None = None,
    province: str | None = None,
    radius_km: float = 25.0,
    min_area_m2: float | None = None,
    max_area_m2: float | None = None,
    max_price_eur: float | None = None,
    max_results: int = 40,
) -> dict[str, Any]:
    """Consulta las fuentes y devuelve candidatas sin guardarlas.

    Sin provincia ni coordenadas la búsqueda es nacional: las Subastas del BOE
    admiten buscar en toda España, y limitarlo artificialmente dejaba fuera la
    única fuente que de verdad responde.
    """
    candidates: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    # La API de datos abiertos del BOE va primero: es la vía documentada y sin
    # clave, y toda subasta pasa por el Boletín antes de existir en el portal.
    # Un único recorrido de boletines alimenta las dos fuentes que salen de
    # ahí: pedir dos veces los mismos cien anuncios al día era lo que hacía
    # que el BOE empezara a cortar peticiones a mitad de camino.
    recorrido = _recorrer_boletines(max_results)
    attempts.append(_from_boe_api(province, max_results, candidates, recorrido))
    attempts.append(_from_boe_anuncios(province, max_results, candidates, recorrido))

    # Y detrás el buscador del portal, que no está documentado pero filtra por
    # provincia y no obliga a recorrer boletines día a día.
    attempts.append(_from_boe(province, lat, lon, max_results, candidates))

    if lat is not None and lon is not None:
        for source in [IdealistaSource(), *iter_rapidapi_sources()]:
            attempts.append(
                _from_listings_source(
                    source, lat, lon, radius_km,
                    min_area_m2, max_area_m2, max_price_eur, candidates,
                )
            )
    else:
        # Decirlo es mejor que dejar la tabla a medias sin explicación.
        for source in [IdealistaSource(), *iter_rapidapi_sources()]:
            attempts.append({
                "source": source.key, "name": source.name,
                "radius_note": getattr(source, "radius_note", ""),
                "skipped": "Busca por coordenadas: indica un punto y un radio.",
            })

    for candidate in candidates:
        candidate["token"] = _token(candidate)
        candidate["already_saved"] = _exists(db, candidate)

    filtered = _sin_repetidas(
        [c for c in candidates if _passes(c, min_area_m2, max_area_m2, max_price_eur)]
    )
    # Lo más barato por metro primero: es el criterio de la aplicación.
    filtered.sort(key=lambda c: c.get("price_eur_m2") or 0.0)

    log_operation("descubrir", candidatas=len(filtered), fuentes=len(attempts))
    return {
        "candidates": filtered[:max_results],
        "total_found": len(filtered),
        "attempts": attempts,
        "sources_ok": sum(1 for a in attempts if a.get("found")),
    }


def _sin_repetidas(candidatas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Una misma subasta la encuentran varias fuentes.

    La API de sumarios y el buscador del portal devuelven los mismos lotes, y
    sin esto salían duplicados en la tabla y se importaban dos veces. Gana la
    primera, que viene de la fuente más fiable porque es la que se consulta
    antes.
    """
    unicas: dict[tuple[str, str], dict[str, Any]] = {}
    for candidata in candidatas:
        clave = (str(candidata.get("source")), str(candidata.get("external_id")))
        unicas.setdefault(clave, candidata)
    return list(unicas.values())


def _passes(
    candidate: dict[str, Any],
    min_area_m2: float | None,
    max_area_m2: float | None,
    max_price_eur: float | None,
) -> bool:
    """Los filtros se aplican aquí porque no toda fuente los admite en su API."""
    area = candidate.get("area_m2") or 0.0
    price = candidate.get("price_eur") or 0.0
    if not area or not price:
        return False
    if min_area_m2 and area < min_area_m2:
        return False
    if max_area_m2 and area > max_area_m2:
        return False
    if max_price_eur and price > max_price_eur:
        return False
    return True


def _from_boe(
    province: str | None, lat: float | None, lon: float | None,
    max_results: int, out: list[dict[str, Any]],
) -> dict[str, Any]:
    source = BoeSubastasSource()
    info: dict[str, Any] = {
        "source": source.key,
        "name": source.name,
        "radius_note": getattr(source, "radius_note", ""),
        "scope": f"provincia {province}" if province else "toda España",
    }
    try:
        # Se piden los intentos y no search() para poder decir cual de las
        # estrategias respondio: cuando el portal no devuelve nada, saberlo es
        # la diferencia entre depurar y adivinar.
        intentos = source.search_attempts(province, min(max_results, 25))
    except SourceError as exc:
        info["error"] = str(exc)[:250]
        return info

    # Se pasa tambien el cuerpo de la respuesta cuando no hay resultados: es
    # lo unico que dice si el portal contesto "no hay nada", un error o una
    # pagina de sesion caducada, y sin verlo solo cabe adivinar.
    info["strategies"] = [
        {"name": nombre, "ids": len(ids),
         "method": detalle.get("method"),
         "http_status": detalle.get("http_status"),
         "bytes": detalle.get("bytes"),
         "cookies": detalle.get("cookies"),
         "form_fields": detalle.get("form_fields"),
         "province_slot": detalle.get("province_slot"),
         "form_action": detalle.get("form_action"),
         "url": detalle.get("url"),
         "final_url": detalle.get("final_url"),
         "redirects": detalle.get("redirects"),
         "headings": detalle.get("headings"),
         "patterns": detalle.get("link_patterns"),
         "excerpt": detalle.get("body_excerpt"),
         "error": detalle.get("error")}
        for nombre, ids, detalle in intentos
    ]
    identifiers: list[str] = []
    for _, ids, _ in intentos:
        if ids:
            identifiers = ids[:max_results]
            break
    info["auctions_found"] = len(identifiers)

    found = 0
    for identifier in identifiers:
        try:
            detail = source.detail(identifier)
        except SourceError:
            continue
        if not source.is_land(detail):
            continue
        item = source.normalize(detail)
        if item:
            item["source_name"] = source.name
            out.append(item)
            found += 1
    info["found"] = found
    return info


def _recorrer_boletines(max_results: int) -> dict[str, Any]:
    """Un solo paseo por los boletines recientes, compartido por dos fuentes."""
    sumario = BoeSumarioSource()
    try:
        return sumario.recent_auction_ids(max_results=min(max_results, 25))
    except SourceError as exc:
        return {"error": str(exc)[:250], "ids": [], "days": [], "announcements": []}


def _from_boe_api(
    province: str | None, max_results: int, out: list[dict[str, Any]],
    recorrido: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Subastas electrónicas anunciadas en el Boletín, con ficha en el portal."""
    sumario = BoeSumarioSource()
    portal = BoeSubastasSource()
    info: dict[str, Any] = {
        "source": sumario.key,
        "name": sumario.name,
        "radius_note": getattr(sumario, "radius_note", ""),
        "scope": f"provincia {province}" if province else "toda España",
    }
    hallazgo = recorrido if recorrido is not None else _recorrer_boletines(max_results)
    if hallazgo.get("error"):
        info["error"] = hallazgo["error"]
        return info

    info["days"] = hallazgo["days"]
    info["auctions_found"] = len(hallazgo["ids"])

    found = 0
    for identificador in hallazgo["ids"]:
        try:
            detail = portal.detail(identificador)
        except SourceError:
            continue
        if not portal.is_land(detail):
            continue
        # El Boletín no filtra por provincia; la ficha del portal sí la trae.
        if province and not _misma_provincia(detail.get("province", ""), province):
            continue
        item = portal.normalize(detail)
        if item:
            item["source_name"] = sumario.name
            out.append(item)
            found += 1
    info["found"] = found
    return info


def _from_boe_anuncios(
    province: str | None, max_results: int, out: list[dict[str, Any]],
    recorrido: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Suelo leído del texto del anuncio, sin pasar por el Portal de Subastas.

    Es la vía que recoge lo que el camino anterior perdía entero: SEPES, ADIF,
    el INVIED y los ayuntamientos venden por pliego, sus anuncios no llevan
    identificador `SUB-` y por eso ninguno llegaba a ser candidata.
    """
    fuente = BoeAnunciosSource()
    info: dict[str, Any] = {
        "source": fuente.key,
        "name": fuente.name,
        "radius_note": getattr(fuente, "radius_note", ""),
        "scope": f"provincia {province}" if province else "toda España",
    }
    hallazgo = recorrido if recorrido is not None else _recorrer_boletines(max_results)
    if hallazgo.get("error"):
        info["error"] = hallazgo["error"]
        return info

    anuncios = hallazgo.get("announcements") or []
    info["announcements_read"] = len(anuncios)
    resultado = fuente.from_crawl(
        anuncios, province=province, max_results=max_results
    )
    # Los descartes se enseñan porque son la mitad del diagnóstico: saber que
    # se leyeron ochenta anuncios y setenta no eran de suelo es una respuesta,
    # y "0 encontradas" no lo es.
    info["discarded"] = resultado["discarded"]
    out.extend(resultado["candidates"])
    info["found"] = len(resultado["candidates"])
    return info


def _misma_provincia(de_la_ficha: str, pedida: str) -> bool:
    from app.provinces import code_for

    codigo = code_for(pedida)
    return codigo is not None and code_for(de_la_ficha) == codigo


def _from_listings_source(
    source: Any, lat: float, lon: float, radius_km: float,
    min_area_m2: float | None, max_area_m2: float | None,
    max_price_eur: float | None, out: list[dict[str, Any]],
) -> dict[str, Any]:
    info: dict[str, Any] = {"source": source.key, "name": source.name}
    try:
        items = source.search_lands(
            lat, lon, radius_km,
            min_size_m2=min_area_m2, max_size_m2=max_area_m2,
            max_price=max_price_eur, max_pages=1,
        )
    except (SourceError, NotImplementedError) as exc:
        info["error"] = str(exc)[:250]
        return info
    for item in items:
        item["source_name"] = source.name
    out.extend(items)
    info["found"] = len(items)
    return info


def import_candidates(db: Session, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Guarda las candidatas elegidas, con geometria y puntuacion."""
    from app.services import analyze_listing

    created = updated = skipped = 0
    for payload in payloads:
        item = {k: v for k, v in payload.items() if k in LISTING_FIELDS}
        cadastral = item.pop("cadastral_ref", None)

        if not item.get("external_id") or not item.get("price_eur") or not item.get("area_m2"):
            skipped += 1
            continue
        item["external_id"] = str(item["external_id"])

        # La referencia catastral da la posicion exacta y la forma real; sin
        # ella el analisis del entorno seria sobre un punto aproximado.
        if cadastral and not item.get("lat"):
            _fill_from_cadastre(item, cadastral)
        if not item.get("lat"):
            resolved = resolve_missing_coords(db, item)
            if resolved is None:
                skipped += 1
                continue
            item["lat"], item["lon"] = resolved
        if cadastral:
            item["cadastral_ref"] = cadastral

        existing = db.execute(
            select(Listing).where(
                Listing.source == item["source"],
                Listing.external_id == item["external_id"],
            )
        ).scalar_one_or_none()
        if existing:
            for key, value in item.items():
                setattr(existing, key, value)
            existing.active = True
            listing = existing
            updated += 1
        else:
            listing = Listing(**item)
            db.add(listing)
            created += 1

        municipality = match_municipality(
            db, item.get("municipality_name", ""), item["lat"], item["lon"]
        )
        if municipality:
            listing.municipality_id = municipality.id

    db.commit()

    # Se puntua al final: comparar contra el mercado de la zona necesita que
    # las recien importadas ya esten en la base.
    analyzed = 0
    for listing in db.execute(select(Listing).where(Listing.active.is_(True))).scalars():
        try:
            analyze_listing(db, listing)
            analyzed += 1
        except Exception:  # una ficha rota no debe frenar la importacion entera
            continue

    log_operation("importar", nuevas=created, actualizadas=updated,
                  descartadas=skipped, analizadas=analyzed)
    return {"created": created, "updated": updated,
            "skipped": skipped, "analyzed": analyzed}


def _fill_from_cadastre(item: dict[str, Any], cadastral_ref: str) -> None:
    from app.sources.catastro import CatastroSource

    try:
        feature = CatastroSource().parcel_geometry(cadastral_ref)
    except Exception:
        return
    ring = extract_ring_safe(feature) if feature else None
    if not ring:
        return
    item["lat"] = sum(p[1] for p in ring) / len(ring)
    item["lon"] = sum(p[0] for p in ring) / len(ring)
    item["parcel_geojson"] = feature


def extract_ring_safe(feature: dict[str, Any]) -> list[list[float]] | None:
    from app.services import _extract_ring

    try:
        return _extract_ring(feature)
    except Exception:
        return None


def resolve_missing_coords(db: Session, item: dict[str, Any]) -> tuple[float, float] | None:
    """Coordenadas del municipio del anuncio, si se reconoce por nombre."""
    name = (item.get("municipality_name") or "").strip()
    if not name:
        return None
    found = db.execute(
        select(Municipality).where(Municipality.name.ilike(name))
    ).scalars().first()
    return (found.lat, found.lon) if found else None


def match_municipality(
    db: Session, name: str, lat: float, lon: float
) -> Municipality | None:
    """Asocia un anuncio a su municipio: primero por nombre, si no por cercania."""
    if name:
        found = db.execute(
            select(Municipality).where(Municipality.name.ilike(name.strip()))
        ).scalars().first()
        if found:
            return found

    delta = 0.25
    candidates = db.execute(
        select(Municipality).where(
            Municipality.lat.between(lat - delta, lat + delta),
            Municipality.lon.between(lon - delta, lon + delta),
        )
    ).scalars().all()
    if not candidates:
        return None

    from app.analysis.geo import haversine_km

    return min(candidates, key=lambda m: haversine_km(lat, lon, m.lat, m.lon))


def _token(candidate: dict[str, Any]) -> str:
    """Identificador estable de una candidata, para seleccionarla en el formulario."""
    raw = f"{candidate.get('source')}|{candidate.get('external_id')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _exists(db: Session, candidate: dict[str, Any]) -> bool:
    return db.execute(
        select(Listing.id).where(
            Listing.source == candidate.get("source"),
            Listing.external_id == str(candidate.get("external_id")),
        )
    ).scalar_one_or_none() is not None


def serialize(candidate: dict[str, Any]) -> str:
    """Empaqueta una candidata para viajar en el formulario de importacion.

    Se manda con la propia fila en vez de guardarla en sesion: evita estado en
    el servidor y que la seleccion caduque mientras el usuario decide.
    """
    ligero = {k: v for k, v in candidate.items() if k in LISTING_FIELDS}
    return json.dumps(ligero, ensure_ascii=False, default=str)


def deserialize(payload: str) -> dict[str, Any] | None:
    """Lee una candidata que vuelve del formulario, sin fiarse de su contenido."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k in LISTING_FIELDS}
