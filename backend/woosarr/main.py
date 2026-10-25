"""FastAPI application factory: versioned API, error handling, SPA serving."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import sys
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from woosarr import __version__, build
from woosarr.api import admins as admins_api
from woosarr.api import api_keys as api_keys_api
from woosarr.api import audit_api, requests_api
from woosarr.api import auth as auth_api
from woosarr.api import dashboard as dashboard_api
from woosarr.api import emoji as emoji_api
from woosarr.api import integrations as integrations_api
from woosarr.api import libraries as libraries_api
from woosarr.api import logs as logs_api
from woosarr.api import oidc as oidc_api
from woosarr.api import reaction_posts as reaction_posts_api
from woosarr.api import roles as roles_api
from woosarr.api import setup as setup_api
from woosarr.api import status as status_api
from woosarr.api import users as users_api
from woosarr.api.deps import require_admin
from woosarr.api.middleware import SecurityHeadersMiddleware
from woosarr.core import app_settings, auth
from woosarr.core.config import Settings
from woosarr.core.logs import configure_logging
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox, load_or_create_key
from woosarr.db.session import Database, make_engine, migrate
from woosarr.plex_gateway import PlexGateway
from woosarr.services import emoji_library, friendly_names, media_requests, stall_watch, updates
from woosarr.services.discord_bot import DiscordService
from woosarr.services.oidc import OidcService
from woosarr.services.plex import PlexService
from woosarr.services.sync import SyncService

log = logging.getLogger("woosarr.app")

# Served from the UI's static folder; not in every Python's mimetypes table.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/manifest+json", ".webmanifest")

API_VERSION = "v1"


def _health(request: Request) -> dict[str, str]:
    b = request.app.state.build
    return {"status": "ok", "version": b.version, "channel": b.channel, "commit": b.short_commit, "api": API_VERSION}


def _announce_setup_code(code: auth.SetupCode) -> None:
    # stdout only: logging would copy it into the log file and the logs API.
    bar = "=" * 64
    sys.stdout.write(
        f"\n{bar}\n WooSarr first-run setup code: {code.value}\n"
        f" Enter it in the web UI to create the first admin account.\n{bar}\n\n"
    )
    sys.stdout.flush()


def _preload_lazy_imports() -> None:
    """anyio imports its file helpers on first use, which is the first static file Starlette serves,
    inside the event loop. On a cold disk that read once took over 10 s, blocked the loop and
    stalled the Discord heartbeat (live log 2026-10-23). Import them now, before anything runs."""
    import anyio

    for name in ("open_file", "Path", "to_thread", "wrap_file"):
        getattr(anyio, name)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    _preload_lazy_imports()
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
        stored_settings = app_settings.load(s)

    plex_service: PlexService | None = None
    discord_service: DiscordService | None = None
    emoji_lib = emoji_library.EmojiLibrary(settings.config_dir / "cache" / "emoji", demo=settings.demo)
    if settings.demo:
        from woosarr.dev import demo

        demo_gateway: PlexGateway = demo.make_gateway()
        demo.seed_discord(db)
        log.warning("DEMO MODE: fake Plex server and Discord data; nothing real is touched")

        def gateway() -> PlexGateway | None:
            return demo_gateway
    else:
        plex_service = PlexService(db, secret_store)
        gateway = plex_service.gateway
    sync = SyncService(db, gateway)
    this_build = build.current()
    update_checker = updates.UpdateChecker(this_build)

    def update_check_on() -> bool:
        with db.session() as s:
            return app_settings.load(s).update_check

    request_service = media_requests.RequestService(db, secret_store)
    if not settings.demo:
        discord_service = DiscordService(db, secret_store, sync.trigger, plex_service, sync, request_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stall_watch.WATCH.start(asyncio.get_running_loop())
        sync.start()
        if gateway() is not None:
            sync.trigger(None, "startup", delay=1)
        poller = None
        emoji_refresh = None
        if not settings.demo:
            emoji_refresh = asyncio.create_task(emoji_library.run_refresher(emoji_lib), name="woosarr-emoji-refresh")
        names_refresh = asyncio.create_task(friendly_names.run_refresher(db, discord_service),
                                            name="woosarr-friendly-names")
        update_task = None
        if update_checker.checkable and not settings.demo:
            update_task = asyncio.create_task(updates.run_checker(update_checker, update_check_on),
                                              name="woosarr-update-check")
        if discord_service is not None:
            await discord_service.start()
            poller = asyncio.create_task(media_requests.run_poller(request_service, discord_service.send_notice),
                                         name="woosarr-request-poll")
        yield
        for task in (poller, emoji_refresh, names_refresh, update_task):
            if task is not None:
                task.cancel()
        if discord_service is not None:
            await discord_service.stop()
        sync.stop()
        stall_watch.WATCH.stop()
        log.info("shutdown complete")

    app = FastAPI(
        title="WooSarr", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.db = db
    app.state.secrets = secret_store
    app.state.sync = sync
    app.state.oidc = OidcService()
    app.state.plex_gateway = gateway
    app.state.plex = plex_service
    app.state.discord = discord_service
    app.state.emoji_library = emoji_lib
    app.state.build = this_build
    app.state.updates = update_checker
    app.state.emoji_guild = discord_service if discord_service is not None else demo.DemoEmojiGuild()
    app.state.requests = request_service
    app.state.plex_status = (
        plex_service.status if plex_service else lambda: {"configured": True, "connected": True, "detail": "demo data"}
    )
    app.state.discord_status = (
        discord_service.status if discord_service
        else lambda: {"configured": True, "connected": True, "detail": "demo data"}
    )
    status_api.apply_runtime_settings(app, stored_settings)
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
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="WooSarr API",
                                   swagger_favicon_url="/favicon.svg")

    v1 = APIRouter(prefix=f"/api/{API_VERSION}")
    v1.add_api_route("/health", _health, methods=["GET"], tags=["health"])
    for module in (auth_api, setup_api, admins_api, api_keys_api, logs_api, status_api, users_api,
                   roles_api, libraries_api, audit_api, integrations_api, reaction_posts_api, requests_api, dashboard_api,
                   emoji_api):
        v1.include_router(module.router)
    v1.include_router(oidc_api.public)
    v1.include_router(oidc_api.admin)
    app.include_router(v1)

    _mount_spa(app, settings)
    log.info(
        "WooSarr %s starting config_dir=%s port=%s log_level=%s",
        this_build.label(),
        settings.config_dir,
        settings.port,
        settings.log_level,
    )
    log.info("CPU access: WooSarr %s", stall_watch.cpu_access())
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
