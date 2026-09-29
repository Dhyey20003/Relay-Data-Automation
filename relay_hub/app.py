"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, __version__
from .api import admin_routes, auth_routes, employee_routes, fetch_routes, import_routes, photo_routes, task_routes
from .config import resource_dir
from .context import AppContext
from .errors import AppError

log = logging.getLogger("relay_hub")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 4 * 1024 * 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _host_only(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if not ctx.started:
            ctx.startup()
        yield
        ctx.shutdown()

    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = _host_only(request.headers.get("host", ""))
        allowed = set(LOOPBACK_HOSTS)
        if ctx.config.lan_enabled:
            allowed |= {h.lower() for h in ctx.config.allowed_hosts}
            if ctx.config.host not in ("0.0.0.0", "::", ""):
                allowed.add(ctx.config.host.lower())
        if host not in allowed:
            return JSONResponse({"error": "This address is not allowed. Office-network access must be enabled by an "
                                          "administrator (Settings > Network) with this host name listed."},
                                status_code=400)
        path = request.url.path
        if ctx.maintenance and path.startswith("/api/") and path != "/api/health":
            return JSONResponse({"error": ctx.maintenance}, status_code=503)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY:
            return JSONResponse({"error": "Upload too large."}, status_code=413)
        if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
            if request.headers.get("x-requested-with") != "RelayHub":
                return JSONResponse({"error": "Request blocked (missing application header)."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != "null":
                ohost = _host_only(origin.split("://", 1)[-1].split("/", 1)[0])
                if ohost != host:
                    return JSONResponse({"error": "Cross-site request blocked."}, status_code=403)
        ctx.enter_request()
        try:
            response = await call_next(request)
        finally:
            ctx.exit_request()
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(_request, exc: AppError):
        body = {"error": exc.message}
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x not in ("body", "query"))
            msgs.append(f"{loc}: {e.get('msg')}" if loc else e.get("msg"))
        return JSONResponse({"error": "Invalid request: " + "; ".join(msgs)}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(_request, exc: Exception):
        log.exception("Unhandled error")
        return JSONResponse({"error": f"Unexpected error: {exc.__class__.__name__}: {exc}. Details were written to the "
                                      "log file."}, status_code=500)

    for r in (auth_routes, employee_routes, photo_routes, import_routes, task_routes, admin_routes, fetch_routes):
        app.include_router(r.router)

    static = resource_dir() / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(static / "img" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/{rest:path}", include_in_schema=False)
    def spa(rest: str):
        if rest.startswith("api/") or "." in Path(rest).name:
            raise HTTPException(404, "Not found")
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})

    return app
