"""FastAPI application: routes, error mapping and wiring."""

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import models
from app.catalog import Catalog, query_meters
from app.config import Settings, get_settings
from app.errors import InvalidRequest, NotFound, ServiceError
from app.portal.client import PortalClient
from app.service import MeterService

API_DESCRIPTION = """
A clean, read-only JSON API over the **Urja Meter Ops** portal (smart-meter details,
network hierarchy and consumption), which has no API of its own.

* Meter, transformer and hierarchy endpoints are served from a catalogue built from the
  portal's bulk export and refreshed every 15 minutes (`GET /api/v1/snapshot` shows its age).
* Consumption is fetched live from the portal per request (cached for 5 minutes).
* Network data is repaired where the portal is inconsistent; every correction is listed
  in the meter's `data_quality` field.

Errors always look like `{"error": {"code": "...", "message": "..."}}`.
"""

MeterId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,32}$", examples=["J100000"])]
ERRORS = {404: {"model": models.ErrorResponse}, 502: {"model": models.ErrorResponse}, 503: {"model": models.ErrorResponse}}


def create_app(settings: Settings | None = None, client: PortalClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or get_settings()
        portal = client or PortalClient(cfg)
        app.state.service = MeterService(portal, Catalog(portal, cfg.snapshot_ttl_s), cfg.consumption_ttl_s)
        yield
        await portal.aclose()

    app = FastAPI(title="Urja Meter Ops API", version="1.0.0", description=API_DESCRIPTION, lifespan=lifespan)
    _install_error_handlers(app)
    _install_routes(app)
    return app


def _service(request: Request) -> MeterService:
    return request.app.state.service


def _install_error_handlers(app: FastAPI) -> None:
    def error(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
        body = {"code": code, "message": message}
        if details is not None:
            body["details"] = details
        return JSONResponse({"error": body}, status_code=status, headers=headers)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        if exc.status_code >= 500:
            logging.getLogger(__name__).warning("%s: %s", exc.code, exc.message)
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return error(exc.status_code, exc.code, exc.message, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        details = [{"loc": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return error(422, "invalid_request", "request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return error(exc.status_code, "not_found" if exc.status_code == 404 else "http_error", str(exc.detail))


def _install_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["ops"], summary="Liveness check (does not call the portal)")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/meters", response_model=models.MeterPage, tags=["meters"], responses=ERRORS, summary="List and filter meters")
    async def list_meters(
        request: Request,
        q: Annotated[str | None, Query(description="Case-insensitive substring of meter ID or serial number.")] = None,
        status: Annotated[str | None, Query(examples=["faulty"])] = None,
        make: str | None = None,
        phase: Annotated[str | None, Query(examples=["three"])] = None,
        install_type: Annotated[str | None, Query(examples=["ct_operated"])] = None,
        record_format: Annotated[str | None, Query(examples=["v2"])] = None,
        dt: Annotated[str | None, Query(description="Distribution transformer code.", examples=["DT-001"])] = None,
        node: Annotated[
            str | None, Query(description="Hierarchy node id (from /hierarchy); returns all meters beneath it.", examples=["Z-01/C-01"])
        ] = None,
        lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
        lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
        radius_km: Annotated[float, Query(gt=0, le=100, description="Used with lat/lng; results sorted by distance.")] = 1.0,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        if (lat is None) != (lng is None):
            raise InvalidRequest("lat and lng must be given together")
        snapshot = await _service(request).catalog.get()
        results = query_meters(
            snapshot,
            q=q,
            status=status,
            make=make,
            phase=phase,
            install_type=install_type,
            record_format=record_format,
            dt=dt,
            node=node,
            near=(lat, lng, radius_km) if lat is not None else None,
        )
        start = (page - 1) * page_size
        data = [{**meter, "distance_km": round(d, 3) if d is not None else None} for meter, d in results[start : start + page_size]]
        return {"data": data, "page": page, "page_size": page_size, "total": len(results), "as_of": snapshot.fetched_at}

    @app.get("/api/v1/meters/{meter_id}", response_model=models.MeterDetail, tags=["meters"], responses=ERRORS, summary="Get one meter")
    async def get_meter(request: Request, meter_id: MeterId):
        return await _service(request).get_meter(meter_id)

    @app.get(
        "/api/v1/meters/{meter_id}/consumption",
        response_model=models.Consumption,
        tags=["meters"],
        responses=ERRORS,
        summary="Meter readings and consumption for a date range",
        description=(
            "Dates are inclusive, in IST. Without `from`/`to` the portal's default applies: the 7 days up to its latest "
            "reading. With only one bound, the portal returns a 7-day window anchored on it. The portal currently holds "
            "data for June 2026 only. Most meters report daily; some report half-hourly (see `interval_minutes`)."
        ),
    )
    async def get_consumption(
        request: Request,
        meter_id: MeterId,
        date_from: Annotated[date | None, Query(alias="from", examples=["2026-06-01"])] = None,
        date_to: Annotated[date | None, Query(alias="to", examples=["2026-06-30"])] = None,
    ):
        return await _service(request).get_consumption(meter_id, date_from, date_to)

    @app.get("/api/v1/transformers", response_model=models.TransformerList, tags=["network"], responses=ERRORS, summary="List distribution transformers")
    async def list_transformers(request: Request):
        snapshot = await _service(request).catalog.get()
        data = sorted(snapshot.transformers.values(), key=lambda t: t["code"])
        return {"data": data, "total": len(data), "as_of": snapshot.fetched_at}

    @app.get(
        "/api/v1/transformers/{code}",
        response_model=models.TransformerDetail,
        tags=["network"],
        responses=ERRORS,
        summary="Get one transformer and its meters",
    )
    async def get_transformer(request: Request, code: Annotated[str, Path(examples=["DT-001"])]):
        snapshot = await _service(request).catalog.get()
        if code not in snapshot.transformers:
            raise NotFound(f"transformer {code} not found")
        return snapshot.transformers[code]

    @app.get("/api/v1/hierarchy", response_model=models.Hierarchy, tags=["network"], responses=ERRORS, summary="Network hierarchy tree")
    async def hierarchy(
        request: Request,
        root: Annotated[str | None, Query(description="Node id to start from; defaults to all zones.", examples=["Z-01/C-01"])] = None,
        depth: Annotated[int, Query(ge=1, le=7, description="Levels to include below the root(s).")] = 7,
    ):
        snapshot = await _service(request).catalog.get()
        if root is None:
            roots = snapshot.tree
        elif root in snapshot.nodes:
            roots = [snapshot.nodes[root]]
        else:
            raise NotFound(f"hierarchy node {root} not found")
        return {"data": [_trim(n, depth) for n in roots], "as_of": snapshot.fetched_at}

    @app.get("/api/v1/snapshot", response_model=models.SnapshotStatus, tags=["ops"], summary="Catalogue freshness and data-quality summary")
    async def snapshot_status(request: Request):
        return _service(request).catalog.status()

    @app.post("/api/v1/snapshot/refresh", response_model=models.SnapshotStatus, tags=["ops"], responses=ERRORS, summary="Reload the catalogue from the portal now")
    async def snapshot_refresh(request: Request):
        catalog = _service(request).catalog
        await catalog.refresh()
        return catalog.status()


def _trim(node: dict, depth: int) -> dict:
    children = [_trim(c, depth - 1) for c in node["children"]] if depth > 1 else []
    return {**node, "children": children}


app = create_app()
