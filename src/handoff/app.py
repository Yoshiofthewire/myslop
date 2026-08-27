"""Application factory, security headers, and per-request database connections."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from handoff import db
from handoff.store import Invalid, NotFound

CSP = (
    "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

MAX_BODY_BYTES = 10 * 1024 * 1024


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def create_app(db_path: str, ttl_days: int = 7) -> FastAPI:
    from handoff import api, web

    # docs_url is disabled: script-src 'none' blocks Swagger UI's JS, so it would render
    # as a permanently broken page. openapi_url stays at its default so agents can still
    # fetch /openapi.json directly.
    application = FastAPI(title="handoff", docs_url=None, redoc_url=None)
    application.state.db_path = db_path
    application.state.ttl_days = ttl_days

    boot = db.connect(db_path)
    db.init_schema(boot)
    db.reap(boot)
    boot.close()

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            response = JSONResponse({"detail": "body too large"}, status_code=413)
        else:
            response = await call_next(request)
        # setdefault, not assignment: the blob route sets its own stricter sandbox CSP
        # and must not have it overwritten on the way out. Applied to every response
        # this middleware returns, including the early 413, not just the call_next path.
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @application.exception_handler(Invalid)
    def _invalid(request: Request, exc: Invalid):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @application.exception_handler(NotFound)
    def _not_found(request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @application.exception_handler(web.LoginRequired)
    def _login_required(request: Request, exc: web.LoginRequired):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/login", status_code=303)

    application.include_router(api.router)
    application.include_router(web.router)
    application.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    return application
