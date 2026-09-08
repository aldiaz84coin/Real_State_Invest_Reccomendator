"""Aplicacion FastAPI: API JSON + interfaz web."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, init_db
from app.models import Listing, Municipality, OpportunityScore, Poi, RentalStat, Simulation
from app.schemas import (
    IngestListingsRequest,
    IngestPoisRequest,
    IngestRentalRequest,
    OpportunityQuery,
    SimulationRequest,
)
from app.services import analyze_listing, run_full_simulation
from app.simulation.catalog import (
    CATALOG, IMAGE_EXTENSIONS, get_model, image_path_for, list_models,
)
from app.simulation.presets import DEFAULT_PRESET_ID, get_preset, list_presets
from app.simulation.render_model import render_model_card_svg
from app.simulation.costs import CostAssumptions
from app.simulation.siteplan import SitePlanOptions
from app.sources.base import SourceError
from app.sources.boe import BoeSubastasSource
from app.sources.catastro import CatastroSource
from app.sources.idealista import IdealistaSource
from app.sources.images import ReferenceImageSource
from app.sources.osm import OverpassSource
from app.sources.rapidapi import extract_listings, iter_rapidapi_sources
from app.sources.registry import check_all
from app.sources.rental import InsideAirbnbSource

logger = logging.getLogger("investment")
BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    logger.info("Base de datos lista")
    yield


app = FastAPI(
    title="Recomendador de Inversion Inmobiliaria",
    description=(
        "Localiza terrenos infravalorados en zonas con suelo al alza y cerca de "
        "playa, montana o puntos turisticos; simula la inversion con una vivienda "
        "prefabricada y genera el plan de negocio de alquiler turistico."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


def _euro(value: Any) -> str:
    try:
        return f"{float(value):,.0f} €".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any, decimals: int = 1) -> str:
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["euro"] = _euro
templates.env.filters["pct"] = _pct
templates.env.filters["tojson_safe"] = lambda v: json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------- salud


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    """Sonda de salud para Fly. No toca la red externa a proposito."""
    return {"status": "ok"}


@app.get("/api/sources/health", tags=["fuentes"])
def sources_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Comprueba en vivo el acceso a cada fuente externa.

    Esta es la respuesta definitiva a "que fuentes podemos usar": se ejecuta
    desde el entorno real de despliegue, no desde un sandbox con la salida
    restringida.
    """
    return check_all(db)


# ---------------------------------------------------------------- catalogo


@app.get("/api/prefab-models", tags=["simulacion"])
def prefab_models() -> dict[str, Any]:
    return {"models": list_models()}


@app.get("/api/prefab-models/{model_id}/preview.svg", tags=["simulacion"])
def prefab_model_preview(model_id: str) -> Response:
    """Alzado y planta del modelo, dibujados a escala desde sus dimensiones."""
    try:
        model = get_model(model_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return Response(
        content=render_model_card_svg(model),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Sólo formatos de imagen habituales en web: nada de SVG, que puede llevar
# scripts, ni de tipos arbitrarios.
ALLOWED_IMAGE_TYPES = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def _model_image_url(model_id: str) -> str | None:
    """Foto real del modelo: primero una URL configurada, luego la subida."""
    configured = get_settings().prefab_image_url(model_id)
    if configured:
        return configured
    if image_path_for(model_id):
        return f"/api/prefab-models/{model_id}/image"
    return None


def _model_cards() -> list[dict[str, Any]]:
    """Catálogo con la ficha dibujada y, si la hay, la foto real."""
    return [
        {
            **model.as_dict(),
            "preview_svg": render_model_card_svg(model),
            "image_url": _model_image_url(model.id),
        }
        for model in CATALOG
    ]


@app.get("/api/prefab-models/{model_id}/image", tags=["simulacion"])
def prefab_model_image(model_id: str) -> FileResponse:
    """Sirve la foto real subida para un modelo."""
    get_model(model_id)  # valida contra el catálogo: nada de rutas arbitrarias
    path = image_path_for(model_id)
    if path is None:
        raise HTTPException(404, f"No hay foto subida para «{model_id}».")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/prefab-models/{model_id}/image", tags=["simulacion"])
async def upload_prefab_model_image(
    model_id: str, file: UploadFile = File(...)
) -> dict[str, Any]:
    """Sube la foto real de un modelo.

    Se guarda en el volumen y no en la imagen del contenedor, así que se puede
    añadir sin volver a desplegar. El esquema a escala sigue disponible: la
    foto enseña el acabado y el esquema, las proporciones.
    """
    try:
        model = get_model(model_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None

    extension = ALLOWED_IMAGE_TYPES.get((file.content_type or "").lower())
    if extension is None:
        raise HTTPException(
            415,
            f"Formato no admitido ({file.content_type}). "
            f"Usa {', '.join(sorted(ALLOWED_IMAGE_TYPES))}.",
        )

    settings = get_settings()
    content = await file.read(settings.max_image_bytes + 1)
    if len(content) > settings.max_image_bytes:
        raise HTTPException(
            413, f"La imagen supera el máximo de {settings.max_image_bytes // 1024 // 1024} MB."
        )
    if not content:
        raise HTTPException(422, "El fichero está vacío.")

    directory = Path(settings.model_images_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # Se borran las demás extensiones para que no queden dos fotos del mismo
    # modelo y gane la que resuelva primero el orden de búsqueda.
    for other in IMAGE_EXTENSIONS:
        (directory / f"{model_id}{other}").unlink(missing_ok=True)
    destination = directory / f"{model_id}{extension}"
    destination.write_bytes(content)

    return {
        "model_id": model.id,
        "stored_as": destination.name,
        "bytes": len(content),
        "url": f"/api/prefab-models/{model_id}/image",
    }


def _fetch_model_image(model_id: str, url: str | None = None) -> dict[str, Any]:
    """Descarga y guarda la foto de un modelo desde su anuncio.

    Es una función normal y no una ruta porque la llaman dos endpoints: invocar
    una ruta desde otra hace que los valores por defecto de FastAPI (Query,
    File) lleguen como objetos en vez de como valores.
    """
    try:
        model = get_model(model_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None

    page_url = url or model.reference_url
    if not page_url:
        raise HTTPException(
            422,
            f"«{model.name}» no tiene anuncio de referencia en el catálogo. "
            "Pasa una URL con ?url=... o sube la foto a mano.",
        )

    try:
        content, extension = ReferenceImageSource().fetch(page_url)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from None

    settings = get_settings()
    directory = Path(settings.model_images_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for other in IMAGE_EXTENSIONS:
        (directory / f"{model_id}{other}").unlink(missing_ok=True)
    destination = directory / f"{model_id}{extension}"
    destination.write_bytes(content)

    return {
        "model_id": model.id,
        "source_url": page_url,
        "stored_as": destination.name,
        "bytes": len(content),
        "url": f"/api/prefab-models/{model_id}/image",
    }


@app.post("/api/prefab-models/{model_id}/image/fetch", tags=["simulacion"])
def fetch_prefab_model_image(
    model_id: str,
    url: str | None = Query(None, description="URL alternativa; por defecto, la del catálogo"),
) -> dict[str, Any]:
    """Trae la foto del anuncio de referencia del modelo.

    La descarga la hace el servidor, no el navegador: así la imagen queda
    guardada y servida por la propia app. Enlazarla directamente daría fotos
    rotas, porque las tiendas rechazan las peticiones que no vienen de su web.
    """
    return _fetch_model_image(model_id, url)


@app.post("/api/prefab-models/images/fetch-all", tags=["simulacion"])
def fetch_all_prefab_images() -> dict[str, Any]:
    """Intenta traer la foto de todos los modelos que tengan referencia."""
    resultados = []
    for model in CATALOG:
        if not model.reference_url:
            resultados.append({"model_id": model.id, "skipped": "sin anuncio de referencia"})
            continue
        try:
            resultados.append(_fetch_model_image(model.id))
        except HTTPException as exc:
            # Que una tienda bloquee no debe impedir traer las demás.
            resultados.append({"model_id": model.id, "error": str(exc.detail)[:250]})
    return {
        "results": resultados,
        "ok": sum(1 for r in resultados if r.get("stored_as")),
        "total": len(resultados),
    }


@app.delete("/api/prefab-models/{model_id}/image", tags=["simulacion"])
def delete_prefab_model_image(model_id: str) -> dict[str, Any]:
    """Quita la foto de un modelo; vuelve a mostrarse sólo el esquema."""
    get_model(model_id)
    path = image_path_for(model_id)
    if path is None:
        raise HTTPException(404, f"No hay foto subida para «{model_id}».")
    path.unlink()
    return {"model_id": model_id, "deleted": path.name}


# ---------------------------------------------------------------- oportunidades


@app.get("/api/opportunities", tags=["oportunidades"])
def api_opportunities(
    query: OpportunityQuery = Depends(), db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = _query_opportunities(db, query)
    return {"count": len(rows), "results": rows, "filters": query.model_dump()}


def _query_opportunities(db: Session, query: OpportunityQuery) -> list[dict[str, Any]]:
    """Aplica los filtros del buscador sobre anuncios ya analizados."""
    statement = (
        select(Listing, OpportunityScore)
        .join(OpportunityScore, OpportunityScore.listing_id == Listing.id)
        .where(Listing.active.is_(True))
    )

    if query.min_area_m2:
        statement = statement.where(Listing.area_m2 >= query.min_area_m2)
    if query.max_area_m2:
        statement = statement.where(Listing.area_m2 <= query.max_area_m2)
    if query.max_price_eur:
        statement = statement.where(Listing.price_eur <= query.max_price_eur)
    if query.province:
        statement = statement.where(Listing.province.ilike(f"%{query.province}%"))
    if query.q:
        like = f"%{query.q}%"
        statement = statement.where(
            Listing.title.ilike(like)
            | Listing.municipality_name.ilike(like)
            | Listing.address.ilike(like)
        )
    if query.min_discount_pct:
        statement = statement.where(
            OpportunityScore.discount_vs_market >= query.min_discount_pct / 100.0
        )
    if query.max_beach_km is not None:
        statement = statement.where(OpportunityScore.dist_beach_km <= query.max_beach_km)
    if query.max_mountain_km is not None:
        statement = statement.where(OpportunityScore.dist_mountain_km <= query.max_mountain_km)
    if query.require_rising_trend:
        statement = statement.where(OpportunityScore.cagr_5y > 0)

    statement = statement.order_by(OpportunityScore.total_score.desc()).limit(query.limit)

    results = []
    for listing, score in db.execute(statement).all():
        results.append({
            "id": listing.id,
            "source": listing.source,
            "url": listing.url,
            "title": listing.title or listing.address,
            "municipality": listing.municipality_name,
            "province": listing.province,
            "price_eur": listing.price_eur,
            "area_m2": listing.area_m2,
            "price_eur_m2": listing.price_eur_m2,
            "lat": listing.lat,
            "lon": listing.lon,
            "score": round(score.total_score, 1),
            "discount_pct": round((score.discount_vs_market or 0) * 100, 1),
            "market_median_eur_m2": score.market_median_eur_m2,
            "cagr_pct": round((score.cagr_5y or 0) * 100, 2),
            "dist_beach_km": score.dist_beach_km,
            "dist_mountain_km": score.dist_mountain_km,
            "nearest_poi": score.nearest_poi,
            "thumbnail": (listing.raw or {}).get("thumbnail_url"),
            "components": {
                "undervaluation": score.undervaluation_score,
                "trend": score.trend_score,
                "location": score.location_score,
                "size": score.size_score,
            },
        })
    return results


@app.post("/api/listings/{listing_id}/analyze", tags=["oportunidades"])
def api_analyze_listing(listing_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Anuncio no encontrado")
    _, detail = analyze_listing(db, listing)
    return detail


@app.post("/api/analyze-all", tags=["oportunidades"])
def api_analyze_all(db: Session = Depends(get_db), limit: int = 500) -> dict[str, Any]:
    """Recalcula la puntuacion de todos los anuncios activos."""
    listings = db.execute(
        select(Listing).where(Listing.active.is_(True)).limit(limit)
    ).scalars().all()
    qualified = 0
    for listing in listings:
        breakdown, _ = analyze_listing(db, listing)
        qualified += int(breakdown.qualifies)
    return {"analyzed": len(listings), "qualified": qualified}


# ---------------------------------------------------------------- simulacion


@app.post("/api/simulate", tags=["simulacion"])
def api_simulate(request: SimulationRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _simulate(request, db)


def _simulate(request: SimulationRequest, db: Session) -> dict[str, Any]:
    listing: Listing | None = None
    if request.listing_id:
        listing = db.get(Listing, request.listing_id)
        if listing is None:
            raise HTTPException(404, "Anuncio no encontrado")

    land_price = request.land_price_eur if request.land_price_eur is not None else (
        listing.price_eur if listing else None
    )
    area = request.parcel_area_m2 or (listing.area_m2 if listing else None)
    lat = request.lat if request.lat is not None else (listing.lat if listing else None)
    lon = request.lon if request.lon is not None else (listing.lon if listing else None)

    if land_price is None or area is None or lat is None or lon is None:
        raise HTTPException(
            422,
            "Faltan datos de la parcela: indica listing_id, o bien precio, "
            "superficie y coordenadas.",
        )

    costs = CostAssumptions(
        ccaa=request.ccaa,
        seller_is_business=request.seller_is_business,
        contingency_rate=request.contingency_rate,
        pool_eur=request.pool_eur if request.include_pool else 0.0,
    )
    if request.off_grid:
        # Sin red electrica cerca: se sustituye la acometida por solar aislada.
        costs.electricity_connection_eur = 0.0
        costs.off_grid_solar_eur = 14500.0

    site_options = SitePlanOptions(
        setback_front_m=request.setback_front_m,
        setback_sides_m=request.setback_sides_m,
        max_occupancy_rate=request.max_occupancy_rate,
        include_terrace=request.include_terrace,
        include_parking=request.include_parking,
        include_pool=request.include_pool,
        access_lat=lat,
        access_lon=lon,
    )

    overrides = {
        key: value
        for key, value in {
            "adr_eur": request.adr_eur,
            "occupancy_rate": request.occupancy_rate,
            "zone_type": request.zone_type,
            "management_fee": request.management_fee,
            "quality_premium": request.quality_premium,
            "tax_rate": request.tax_rate,
            "loan_amount_eur": request.loan_amount_eur,
            "loan_rate": request.loan_rate,
            "loan_years": request.loan_years,
            "horizon_years": request.horizon_years,
        }.items()
        if value is not None
    }

    try:
        result = run_full_simulation(
            db,
            listing=listing,
            land_price_eur=land_price,
            parcel_area_m2=area,
            lat=lat,
            lon=lon,
            model_id=request.model_id,
            cost_assumptions=costs,
            rental_overrides=overrides,
            site_options=site_options,
            use_cadastre=request.use_cadastre,
            use_cadastral_area=request.use_cadastral_area,
            )
    except ValueError as exc:
        # Un modelo desconocido llegaba como error 500; es un dato de entrada
        # inválido y debe decirse cuáles son los válidos.
        raise HTTPException(422, str(exc)) from None

    if request.save_as:
        saved = Simulation(
            listing_id=listing.id if listing else None,
            name=request.save_as,
            prefab_model_id=request.model_id,
            inputs=request.model_dump(),
            investment=result["investment"],
            business_plan=result["business_plan"],
            site_plan={k: v for k, v in result["site_plan"].items() if k != "svg"},
        )
        db.add(saved)
        db.commit()
        result["saved_simulation_id"] = saved.id

    return result


# ---------------------------------------------------------------- ingesta


@app.post("/api/ingest/listings", tags=["ingesta"])
def api_ingest_listings(
    request: IngestListingsRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Descarga terrenos de Idealista y los guarda enriquecidos con Catastro."""
    # Orden deliberado: primero la vía oficial; los proveedores de RapidAPI son
    # revendedores no oficiales y sólo entran si la oficial no está disponible.
    providers: list = [IdealistaSource(), *iter_rapidapi_sources()]
    attempts: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    used = ""

    for provider in providers:
        try:
            items = provider.search_lands(
                request.lat,
                request.lon,
                request.radius_km,
                min_size_m2=request.min_size_m2,
                max_size_m2=request.max_size_m2,
                max_price=request.max_price_eur,
                max_pages=request.max_pages,
            )
        except (SourceError, NotImplementedError) as exc:
            attempts.append({"source": provider.key, "error": str(exc)[:300]})
            continue

        if items:
            used = provider.key
            break
        attempts.append({"source": provider.key, "error": "sin resultados"})

    if not used:
        raise HTTPException(
            502,
            {
                "message": "Ninguna fuente de anuncios devolvió resultados.",
                "attempts": attempts,
            },
        )

    created = updated = 0
    coords_from_municipality = coords_from_search_center = 0

    for item in items:
        if not item["external_id"] or item["price_eur"] <= 0 or item["area_m2"] <= 0:
            continue

        # Algunos proveedores no devuelven coordenadas en la busqueda. Antes de
        # renunciar al anuncio se le asigna la posicion del municipio, que ya
        # esta en la base y no cuesta ninguna peticion. Queda marcado para que
        # el analisis sepa que las distancias son aproximadas.
        precision = item.pop("coords_precision", "exact")
        if item.get("lat") is None or item.get("lon") is None:
            resolved = _resolve_missing_coords(db, item)
            if resolved is None:
                item["lat"], item["lon"] = request.lat, request.lon
                precision = "search_center"
                coords_from_search_center += 1
            else:
                item["lat"], item["lon"] = resolved
                precision = "municipality"
                coords_from_municipality += 1
        if isinstance(item.get("raw"), dict):
            item["raw"]["coords_precision"] = precision
        existing = db.execute(
            select(Listing).where(
                Listing.source == item["source"], Listing.external_id == item["external_id"]
            )
        ).scalar_one_or_none()

        if existing:
            for key, value in item.items():
                setattr(existing, key, value)
            existing.active = True
            updated += 1
            listing = existing
        else:
            listing = Listing(**item)
            db.add(listing)
            created += 1

        municipality = _match_municipality(db, item["municipality_name"], item["lat"], item["lon"])
        if municipality:
            listing.municipality_id = municipality.id

    db.commit()
    return {
        "fetched": len(items),
        "created": created,
        "updated": updated,
        "source_used": used,
        "fallbacks_tried": attempts,
        "coords": {
            "from_municipality": coords_from_municipality,
            "from_search_center": coords_from_search_center,
            "note": (
                "Los anuncios sin coordenadas propias se sitúan en el municipio "
                "(o en el centro de búsqueda si no se reconoce). Sus distancias "
                "a playa o montaña son aproximadas."
            ) if (coords_from_municipality or coords_from_search_center) else "",
        },
        "discarded": getattr(
            next((p for p in providers if p.key == used), None), "discarded", {}
        ),
    }


@app.get("/api/sources/rapidapi/probe", tags=["fuentes"])
def api_probe_rapidapi(
    source: str = Query("rapidapi_idealista", description="Clave de la fuente a sondear"),
    lat: float = 36.7213,
    lon: float = -4.4214,
    radius_km: float = 15.0,
) -> dict[str, Any]:
    """Diagnostica el mapeo de un proveedor de RapidAPI contra datos reales.

    Existe porque el esquema de estos revendedores no está documentado de forma
    fiable y cambia sin aviso. Devuelve qué devolvió la API y qué se pudo mapear,
    para poder corregir host, ruta o campos sin ir a ciegas. Nunca expone la
    clave: sólo dice si está configurada.
    """
    provider = next((p for p in iter_rapidapi_sources() if p.key == source), None)
    if provider is None:
        raise HTTPException(
            404,
            f"Fuente '{source}' desconocida. Disponibles: "
            + ", ".join(p.key for p in iter_rapidapi_sources()),
        )
    if not provider.configured:
        raise HTTPException(422, f"{provider.name} no está configurada: falta RAPIDAPI_KEY.")

    # prepare_params, no build_params: si el proveedor necesita resolver la
    # zona antes de buscar, el diagnóstico debe hacer el mismo recorrido que la
    # búsqueda real o comprobaría algo que nunca ocurre.
    try:
        params = provider.prepare_params(lat, lon, radius_km, page=1)
    except Exception as exc:
        # No se lanza un error a secas: se devuelve lo que se sabe, incluida la
        # respuesta cruda de la resolución de zona cuando el proveedor la usa,
        # porque si no el diagnóstico no diagnostica nada.
        diagnosis: dict[str, Any] = {
            "source": provider.key,
            "host": provider.host,
            "stage": "preparacion de parametros",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if hasattr(provider, "suggestions_raw"):
            diagnosis["hint"] = (
                "Este proveedor resuelve la zona antes de buscar. Abajo va la "
                "respuesta cruda de esa llamada."
            )
            try:
                from app.sources.osm import NominatimSource

                place = NominatimSource().reverse(lat, lon)
                municipio = place["municipality"] if place else "Malaga"
                diagnosis["municipality_resolved"] = municipio
                diagnosis["suggestions_response"] = provider.suggestions_raw(municipio)
            except Exception as inner:
                diagnosis["suggestions_error"] = f"{type(inner).__name__}: {inner}"
        return diagnosis

    try:
        response = provider.request(
            provider.search_method,
            f"{provider.base_url()}{provider.search_path}",
            headers=provider.headers(),
            params=params,
        )
    except Exception as exc:
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from None

    diagnosis: dict[str, Any] = {
        "source": provider.key,
        "host": provider.host,
        "path": provider.search_path,
        "params_sent": params,
        "http_status": response.status_code,
    }

    try:
        payload = response.json()
    except ValueError:
        diagnosis["error"] = "La respuesta no es JSON."
        diagnosis["body_preview"] = response.text[:500]
        return diagnosis

    diagnosis["top_level_keys"] = (
        sorted(payload)[:30] if isinstance(payload, dict) else f"lista de {len(payload)}"
    )
    raw_items = extract_listings(payload)
    diagnosis["listings_found"] = len(raw_items)

    if not raw_items:
        # Sin esto habría que adivinar: se enseña un trozo del cuerpo real.
        diagnosis["body_preview"] = json.dumps(payload, ensure_ascii=False)[:1200]
        diagnosis["hint"] = (
            "No se localizó ninguna lista de anuncios. Revisa la ruta "
            "(RAPIDAPI_PATHS) o pásame este cuerpo para ajustar el mapeo."
        )
        return diagnosis

    sample = raw_items[0]
    diagnosis["sample_raw_keys"] = sorted(sample)[:40]
    normalized = provider.normalize(sample)
    diagnosis["sample_normalized"] = normalized
    if normalized is None:
        diagnosis["hint"] = (
            "Se encontraron anuncios pero faltan campos obligatorios "
            "(precio, superficie o coordenadas). Compara sample_raw_keys con "
            "los nombres esperados para ajustar el mapeo."
        )
    else:
        mapped = sum(1 for v in normalized.values() if v not in (None, "", 0))
        diagnosis["hint"] = f"Mapeo correcto: {mapped} campos con valor."
    return diagnosis


@app.get("/api/sources/rapidapi/suggestions", tags=["fuentes"])
def api_probe_suggestions(
    query: str = Query(..., description="Municipio o zona a resolver"),
    source: str = Query("rapidapi_fotocasa"),
) -> dict[str, Any]:
    """Devuelve la respuesta cruda de /suggestions de un proveedor.

    Existe porque el diagnóstico anterior era circular: si la resolución de
    zona falla, el probe de búsqueda falla en ese mismo punto y no llega a
    enseñar nada. Aquí se ve el cuerpo tal cual para saber cómo llama este
    proveedor al identificador de zona.
    """
    provider = next((p for p in iter_rapidapi_sources() if p.key == source), None)
    if provider is None or not hasattr(provider, "suggestions_raw"):
        raise HTTPException(
            404, f"La fuente '{source}' no tiene búsqueda de zonas."
        )
    if not provider.configured:
        raise HTTPException(422, f"{provider.name} no está configurada: falta la clave.")
    try:
        return provider.suggestions_raw(query)
    except Exception as exc:
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from None


@app.post("/api/ingest/boe-subastas", tags=["ingesta"])
def api_ingest_boe(
    province: str | None = Query(None, description="Provincia o su código INE"),
    only_land: bool = Query(True, description="Sólo suelo: fincas, solares y parcelas"),
    max_results: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Carga inmuebles del Portal de Subastas del BOE.

    Es la única fuente gratuita de ofertas activas en toda España, y encaja con
    el propósito de la app: en subasta el suelo suele salir por debajo de
    mercado. Muchas subastas traen referencia catastral, lo que permite cruzar
    la parcela con su geometría real aunque el anuncio no lleve coordenadas.
    """
    source = BoeSubastasSource()
    try:
        identifiers = source.search(province, only_land=only_land, max_results=max_results)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from None

    created = updated = skipped_not_land = skipped_incomplete = 0
    errors: list[str] = []

    for identifier in identifiers:
        try:
            detail = source.detail(identifier)
        except SourceError as exc:
            errors.append(f"{identifier}: {str(exc)[:120]}")
            continue

        if only_land and not source.is_land(detail):
            skipped_not_land += 1
            continue

        item = source.normalize(detail)
        if item is None:
            # Sin importe o sin superficie no se puede calcular el €/m².
            skipped_incomplete += 1
            continue

        cadastral = item.pop("cadastral_ref", None)
        item.pop("coords_precision", None)

        # La referencia catastral da la posición exacta sin depender de que la
        # subasta publique coordenadas, que nunca lo hace.
        if cadastral:
            try:
                feature = CatastroSource().parcel_geometry(cadastral)
            except Exception:
                feature = None
            if feature:
                ring = _extract_ring_safe(feature)
                if ring:
                    item["lat"] = sum(p[1] for p in ring) / len(ring)
                    item["lon"] = sum(p[0] for p in ring) / len(ring)
                    item["parcel_geojson"] = feature
                    item["cadastral_ref"] = cadastral

        if item.get("lat") is None:
            resolved = _resolve_missing_coords(db, item)
            if resolved is None:
                skipped_incomplete += 1
                continue
            item["lat"], item["lon"] = resolved

        existing = db.execute(
            select(Listing).where(
                Listing.source == item["source"], Listing.external_id == item["external_id"]
            )
        ).scalar_one_or_none()
        if existing:
            for key, value in item.items():
                setattr(existing, key, value)
            existing.active = True
            updated += 1
            listing = existing
        else:
            listing = Listing(**item)
            db.add(listing)
            created += 1

        municipality = _match_municipality(
            db, item.get("municipality_name", ""), item["lat"], item["lon"]
        )
        if municipality:
            listing.municipality_id = municipality.id

    db.commit()
    return {
        "found": len(identifiers),
        "created": created,
        "updated": updated,
        "skipped_not_land": skipped_not_land,
        "skipped_incomplete": skipped_incomplete,
        "errors": errors[:10],
    }


@app.get("/api/sources/boe/probe", tags=["fuentes"])
def api_probe_boe(
    province: str | None = Query(None),
    id_sub: str | None = Query(None, description="Diagnostica una subasta concreta"),
) -> dict[str, Any]:
    """Diagnostica el parseo del portal de subastas contra datos reales."""
    source = BoeSubastasSource()
    try:
        if id_sub:
            detail = source.detail(id_sub)
            return {
                "id_sub": id_sub,
                "parsed": {k: v for k, v in detail.items() if k != "raw_fields"},
                "all_labels_found": sorted(detail.get("raw_fields", {})),
                "is_land": source.is_land(detail),
                "normalized": source.normalize(detail),
            }
        # Se enseñan todos los intentos, no sólo el resultado final: decir
        # "0 subastas" sin más no permite saber si falla el filtro, la ruta o
        # es que de verdad no hay ninguna.
        attempts = source.search_attempts(province, max_results=20)
        found = next((ids for _, ids, _ in attempts if ids), [])
        return {
            "province": province,
            "province_code": source.province_code(province),
            "auctions_found": len(found),
            "ids": found[:20],
            "attempts": [
                {"strategy": nombre, "ids_found": len(ids), **info}
                for nombre, ids, info in attempts
            ],
            "hint": (
                "Añade ?id_sub=<id> para ver el detalle parseado de una."
                if found else
                "Ninguna estrategia devolvió subastas. En cada intento va el "
                "estado HTTP, el tamaño de la respuesta, cuántos enlaces de "
                "cada forma hay y un extracto del texto de la página."
            ),
        }
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from None


@app.post("/api/ingest/pois", tags=["ingesta"])
def api_ingest_pois(request: IngestPoisRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Carga playas, cumbres y atracciones turisticas desde OpenStreetMap."""
    try:
        pois = OverpassSource().fetch_pois(
            request.lat, request.lon, request.radius_km, request.kinds
        )
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from None

    created = 0
    for item in pois:
        exists = db.execute(
            select(Poi.id).where(
                Poi.source == item["source"], Poi.external_id == item["external_id"]
            )
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(Poi(**item))
        created += 1
    db.commit()
    return {"fetched": len(pois), "created": created}


@app.post("/api/ingest/rental", tags=["ingesta"])
def api_ingest_rental(
    request: IngestRentalRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Importa un volcado de InsideAirbnb y lo agrega a tarifa y ocupacion."""
    municipality = db.execute(
        select(Municipality).where(Municipality.ine_code == request.municipality_ine_code)
    ).scalar_one_or_none()
    if municipality is None:
        raise HTTPException(404, "Municipio desconocido")

    source = InsideAirbnbSource()
    url = request.dataset_url
    try:
        if not url:
            datasets = source.discover_spain_datasets()
            match = next(
                (d for d in datasets if d["city"].lower() in municipality.name.lower()
                 or municipality.name.lower() in d["city"].lower()),
                None,
            )
            if not match:
                raise HTTPException(
                    404,
                    f"InsideAirbnb no publica volcado para {municipality.name}. "
                    f"Disponibles: {', '.join(d['city'] for d in datasets[:20])}",
                )
            url = match["url"]
        rows = source.fetch_listings(url)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from None

    aggregate = source.aggregate(rows)
    if not aggregate["sample_size"]:
        raise HTTPException(422, "El volcado no contiene anuncios utilizables.")

    existing = db.execute(
        select(RentalStat).where(
            RentalStat.municipality_id == municipality.id,
            RentalStat.bedrooms == request.bedrooms,
            RentalStat.source == "insideairbnb",
        )
    ).scalar_one_or_none()
    stat = existing or RentalStat(
        municipality_id=municipality.id, bedrooms=request.bedrooms, source="insideairbnb"
    )
    stat.adr_eur = aggregate["adr_eur"]
    stat.adr_p25_eur = aggregate.get("adr_p25_eur")
    stat.adr_p75_eur = aggregate.get("adr_p75_eur")
    stat.occupancy_rate = aggregate["occupancy_rate"]
    stat.sample_size = aggregate["sample_size"]
    db.add(stat)

    municipality.adr_eur = aggregate["adr_eur"]
    municipality.occupancy_rate = aggregate["occupancy_rate"]
    db.commit()

    return {"municipality": municipality.name, "url": url, **aggregate}


@app.post("/api/listings/{listing_id}/cadastre", tags=["ingesta"])
def api_fetch_cadastre(listing_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Recupera del Catastro la geometria real de la parcela de un anuncio."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Anuncio no encontrado")

    source = CatastroSource()
    try:
        reference = listing.cadastral_ref or source.ref_from_coords(listing.lat, listing.lon)
        if not reference:
            raise HTTPException(404, "El Catastro no devuelve parcela para esas coordenadas.")
        feature = source.parcel_geometry(reference)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from None

    if not feature:
        raise HTTPException(404, "Sin geometria disponible para esa referencia catastral.")

    listing.cadastral_ref = reference
    listing.parcel_geojson = feature
    db.commit()
    return {"cadastral_ref": reference, "feature": feature}


# ---------------------------------------------------------------- interfaz web


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stats = {
        "listings": db.scalar(select(func.count(Listing.id))) or 0,
        "scored": db.scalar(select(func.count(OpportunityScore.id))) or 0,
        "municipalities": db.scalar(select(func.count(Municipality.id))) or 0,
        "pois": db.scalar(select(func.count(Poi.id))) or 0,
        "rental_stats": db.scalar(select(func.count(RentalStat.id))) or 0,
    }
    top = _query_opportunities(db, OpportunityQuery(limit=6, min_discount_pct=0,
                                                   require_rising_trend=False))
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "stats": stats, "top": top,
         "idealista_ready": get_settings().idealista_configured},
    )


@app.get("/fuentes", response_class=HTMLResponse, include_in_schema=False)
def page_sources(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    report = check_all(db)
    return templates.TemplateResponse(
        "sources.html", {"request": request, "report": report}
    )


@app.get("/buscar", response_class=HTMLResponse, include_in_schema=False)
def page_search(
    request: Request,
    db: Session = Depends(get_db),
    q: str | None = None,
    province: str | None = None,
    min_area_m2: float = 300,
    max_price_eur: float | None = None,
    min_discount_pct: float = 20,
    max_beach_km: float | None = None,
    require_rising_trend: bool = False,
) -> HTMLResponse:
    query = OpportunityQuery(
        q=q,
        province=province,
        min_area_m2=min_area_m2,
        max_price_eur=max_price_eur,
        min_discount_pct=min_discount_pct,
        max_beach_km=max_beach_km,
        require_rising_trend=require_rising_trend,
        limit=200,
    )
    results = _query_opportunities(db, query)
    provinces = db.execute(
        select(Listing.province).distinct().where(Listing.province != "")
    ).scalars().all()
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "results": results, "filters": query.model_dump(),
         "provinces": sorted(provinces)},
    )


@app.get("/oportunidad/{listing_id}", response_class=HTMLResponse, include_in_schema=False)
def page_opportunity(
    listing_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Anuncio no encontrado")
    breakdown, detail = analyze_listing(db, listing)
    return templates.TemplateResponse(
        "opportunity.html",
        {"request": request, "listing": listing, "detail": detail,
         "breakdown": breakdown.as_dict(), "models": list_models()},
    )


@app.get("/api/presets", tags=["simulacion"])
def api_presets() -> dict[str, Any]:
    return {"presets": list_presets(), "default": DEFAULT_PRESET_ID}


@app.get("/simular", response_class=HTMLResponse, include_in_schema=False)
def page_simulator(
    request: Request,
    db: Session = Depends(get_db),
    listing_id: int | None = None,
    preset: str | None = None,
) -> HTMLResponse:
    listing = db.get(Listing, listing_id) if listing_id else None
    return templates.TemplateResponse(
        "simulator.html",
        {"request": request, "models": _model_cards(), "listing": listing,
         "result": None, "presets": list_presets(), "preset": get_preset(preset)},
    )


@app.post("/simular", response_class=HTMLResponse, include_in_schema=False)
async def page_simulate(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    form = await request.form()
    payload: dict[str, Any] = {}
    for key, value in form.items():
        if value in ("", None):
            continue
        payload[key] = value
    # Las casillas no marcadas no llegan en el formulario.
    for flag in ("include_pool", "include_terrace", "include_parking",
                 "seller_is_business", "off_grid", "use_cadastre", "use_cadastral_area"):
        payload[flag] = flag in form

    try:
        simulation_request = SimulationRequest(**payload)
        result = _simulate(simulation_request, db)
    except HTTPException as exc:
        return templates.TemplateResponse(
            "simulator.html",
            {"request": request, "models": _model_cards(), "listing": None,
             "result": None, "error": exc.detail,
             "presets": list_presets(), "preset": get_preset(None)},
            status_code=exc.status_code,
        )

    listing = db.get(Listing, simulation_request.listing_id) if simulation_request.listing_id else None
    return templates.TemplateResponse(
        "simulator.html",
        {"request": request, "models": _model_cards(), "listing": listing,
         "result": result, "form": payload,
         "presets": list_presets(), "preset": get_preset(None)},
    )


@app.exception_handler(SourceError)
async def source_error_handler(_request: Request, exc: SourceError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "message": str(exc)})


def _extract_ring_safe(feature: dict[str, Any]) -> list[list[float]] | None:
    from app.services import _extract_ring

    try:
        return _extract_ring(feature)
    except Exception:
        return None


def _resolve_missing_coords(db: Session, item: dict[str, Any]) -> tuple[float, float] | None:
    """Coordenadas del municipio del anuncio, si se reconoce por nombre."""
    name = (item.get("municipality_name") or "").strip()
    if not name:
        return None
    found = db.execute(
        select(Municipality).where(Municipality.name.ilike(name))
    ).scalars().first()
    return (found.lat, found.lon) if found else None


def _match_municipality(
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
