"""FastAPI application factory: versioned API, error handling, SPA serving."""

from __future__ import annotations

import logging
import sys

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from woosarr import __version__
from woosarr.api import admins as admins_api
from woosarr.api import api_keys as api_keys_api
from woosarr.api import auth as auth_api
from woosarr.api import logs as logs_api
from woosarr.api import setup as setup_api
from woosarr.api.deps import require_admin
from woosarr.api.middleware import SecurityHeadersMiddleware
from woosarr.core import auth
from woosarr.core.config import Settings
from woosarr.core.logs import configure_logging
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox, load_or_create_key
from woosarr.db.session import Database, make_engine, migrate

log = logging.getLogger("woosarr.app")

API_VERSION = "v1"


def _health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "api": API_VERSION}


def _announce_setup_code(code: auth.SetupCode) -> None:
    # stdout only: logging would copy it into the log file and the logs API.
    bar = "=" * 64
    sys.stdout.write(
        f"\n{bar}\n WooSarr first-run setup code: {code.value}\n"
        f" Enter it in the web UI to create the first admin account.\n{bar}\n\n"
    )
    sys.stdout.flush()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(settings)

    engine = make_engine(settings.db_path)
    migrate(engine)
    db = Database(engine)
    secret_store = SecretStore(SecretBox(load_or_create_key(settings.config_dir)))
    with db.session() as s:
        secret_store.register_all(s)
        auth.prune_sessions(s)
        needs_setup = not auth.admin_exists(s)

    app = FastAPI(title="WooSarr", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.secrets = secret_store
    app.state.login_limiter = auth.RateLimiter(max_attempts=10, window_seconds=300)
    app.state.setup_limiter = auth.RateLimiter(max_attempts=10, window_seconds=300)
    app.state.setup_code = auth.SetupCode() if needs_setup else None
    if needs_setup:
        if settings.setup_code_required:
            _announce_setup_code(app.state.setup_code)
        log.warning("no admin account yet; open the web UI to finish first-run setup")

    app.add_middleware(SecurityHeadersMiddleware)

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

    # API docs are admin-only like everything else (N10).
    @app.get("/api/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
    def openapi_schema() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False, dependencies=[Depends(require_admin)])
    def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="WooSarr API")

    v1 = APIRouter(prefix=f"/api/{API_VERSION}")
    v1.add_api_route("/health", _health, methods=["GET"], tags=["health"])
    for module in (auth_api, setup_api, admins_api, api_keys_api, logs_api):
        v1.include_router(module.router)
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
        # A missing file (anything with an extension) is a real 404; only
        # extension-less paths are client-side routes that get the SPA shell.
        if "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(status_code=404)
        return FileResponse(root / "index.html")
