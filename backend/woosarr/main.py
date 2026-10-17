"""FastAPI application factory: versioned API, error handling, SPA serving."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from woosarr import __version__
from woosarr.api import logs as logs_api
from woosarr.core.config import Settings
from woosarr.core.logs import configure_logging

log = logging.getLogger("woosarr.app")

API_VERSION = "v1"


def _health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "api": API_VERSION}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)

    app = FastAPI(
        title="WooSarr",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full (redacted) traceback goes to the logs; the client only gets an id
        # to search for, never exception text that might contain internals.
        rid = getattr(request.state, "request_id", "-")
        log.error("unhandled error request_id=%s %s %s", rid, request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            {"error": "internal_error", "request_id": rid},
            status_code=500,
            headers={"X-Request-ID": rid},
        )

    # Unversioned alias kept for the container healthcheck and uptime monitors.
    app.add_api_route("/api/health", _health, methods=["GET"], tags=["health"])

    v1 = APIRouter(prefix=f"/api/{API_VERSION}")
    v1.add_api_route("/health", _health, methods=["GET"], tags=["health"])
    v1.include_router(logs_api.router)
    app.include_router(v1)

    _mount_spa(app, settings)
    log.info(
        "WooSarr %s starting config_dir=%s port=%s log_level=%s",
        __version__,
        settings.config_dir,
        settings.port,
        settings.log_level,
    )
    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    static_dir = settings.static_dir
    if not static_dir.is_dir():
        log.warning("static dir %s not found; UI not served", static_dir)
        return
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    root = static_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(root / "index.html")
