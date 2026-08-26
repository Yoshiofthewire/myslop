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

    application = FastAPI(title="handoff", docs_url="/api/docs", redoc_url=None)
    application.state.db_path = db_path
    application.state.ttl_days = ttl_days

    boot = db.connect(db_path)
    db.init_schema(boot)
    db.reap(boot)
    boot.close()

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.headers.get("content-length", "").isdigit():
            if int(request.headers["content-length"]) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "body too large"}, status_code=413)
        response = await call_next(request)
        # setdefault, not assignment: the blob route sets its own stricter sandbox CSP
        # and must not have it overwritten on the way out.
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

    application.include_router(api.router)
    application.include_router(web.router)
    application.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    return application
