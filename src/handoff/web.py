"""Human-facing HTML routes. Session cookie only -- bearer tokens are never consulted."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from handoff import auth
from handoff.app import get_conn

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=303, headers={"Location": "/login"}, detail="login required")


def require_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Row:
    sid = request.cookies.get(auth.COOKIE_NAME, "")
    user = auth.session_user(conn, sid) if sid else None
    if user is None:
        raise LoginRequired
    return user


def require_csrf(request: Request, token: str) -> None:
    sid = request.cookies.get(auth.COOKIE_NAME, "")
    if not sid or not auth.check_csrf(sid, token):
        raise HTTPException(status_code=403, detail="bad csrf token")


def page(request: Request, name: str, user: sqlite3.Row | None = None, **ctx):
    # csrf is derived only for an already-validated user (i.e. request went through
    # require_user's session_user() lookup) -- never minted from a raw, unvalidated cookie.
    sid = request.cookies.get(auth.COOKIE_NAME, "") if user else ""
    return templates.TemplateResponse(
        request, name, {"user": user, "csrf": auth.csrf_token(sid) if sid else "", **ctx}
    )


@router.get("/login")
def login_form(request: Request):
    return page(request, "login.html", error=None)


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    user = auth.verify_user(conn, username, password)
    if user is None:
        return page(request, "login.html", error="Invalid credentials.")
    sid = auth.create_session(conn, user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        sid,
        httponly=True,
        samesite="lax",
        # Secure whenever TLS is actually in play -- including behind a terminating proxy,
        # because uvicorn runs with proxy_headers=True and honours X-Forwarded-Proto.
        # Over plain http a Secure cookie is simply never sent back, so hardcoding it
        # would break login on the documented tailnet deployment while protecting nothing
        # (tailnet traffic is already WireGuard-encrypted).
        secure=request.url.scheme == "https",
        max_age=auth.SESSION_TTL,
        path="/",
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_csrf(request, csrf)
    sid = request.cookies.get(auth.COOKIE_NAME, "")
    if sid:
        auth.delete_session(conn, sid)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@router.get("/")
def index(request: Request, user: sqlite3.Row = Depends(require_user)):
    return page(request, "index.html", user=user, folders=[])
