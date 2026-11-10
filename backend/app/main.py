"""The dashboard server.

    cd backend
    python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

Create the first admin once (or set AUTOMATION_ADMIN_EMAIL / _PASSWORD):
    python -m app.cli create-admin you@example.com
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .db import Database, init_db
from .deps import SESSION_COOKIE, Context, check_origin, load_session_user
from .routers import account, admin, auth, automations, runs
from .services.jobs import JobManager
from .services.registry import Registry

log = logging.getLogger("automation")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
}

# page route -> (html file, who may open it)
PAGES = {
    "/": ("index.html", "user"),
    "/login": ("login.html", "anonymous"),
    "/signup": ("signup.html", "anonymous"),
    "/runs": ("runs.html", "user"),
    "/account": ("account.html", "user"),
    "/admin": ("admin.html", "admin"),
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    init_db(settings.db_path)
    ctx = Context(settings=settings, db=Database(settings.db_path), registry=Registry(settings.automations_dir))
    ctx.jobs = JobManager(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .cli import ensure_bootstrap_admin
        ensure_bootstrap_admin(ctx)
        for problem in ctx.registry.errors:
            log.error("automation not loaded: %s", problem)
        ctx.jobs.start()
        yield
        ctx.jobs.stop()

    app = FastAPI(title="Automation Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def guard(request: Request, call_next):
        try:
            check_origin(request, ctx)
        except Exception as exc:                       # HTTPException from the CSRF check
            return JSONResponse({"detail": getattr(exc, "detail", "Blocked")}, status_code=403)
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(automations.router)
    app.include_router(runs.router)
    app.include_router(admin.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    # ---- pages: the server decides who may even receive each HTML file
    def make_page(filename: str, audience: str):
        def page(request: Request):
            user, _ = load_session_user(ctx, request.cookies.get(SESSION_COOKIE))
            if audience == "anonymous" and user is not None:
                return RedirectResponse("/", status_code=302)
            if audience in ("user", "admin") and user is None:
                return RedirectResponse("/login", status_code=302)
            if audience == "admin" and user["role"] != "admin":
                return RedirectResponse("/", status_code=302)
            return FileResponse(settings.frontend_dir / filename)
        return page

    for path, (filename, audience) in PAGES.items():
        app.add_api_route(path, make_page(filename, audience), methods=["GET"], include_in_schema=False)
    app.add_api_route("/a/{slug}", make_page("automation.html", "user"), methods=["GET"], include_in_schema=False)

    if settings.frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=settings.frontend_dir / "static"), name="static")
    return app

