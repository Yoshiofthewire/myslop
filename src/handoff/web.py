"""Human-facing HTML routes. Session cookie only -- bearer tokens are never consulted."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from handoff import auth, clock, db, store
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
    # Must be called from inside a route body, after `user: ... = Depends(require_user)`
    # has already resolved as a declared dependency -- never itself declared as a
    # Depends(...) parameter. That ordering is what makes a logged-out POST redirect
    # (303, from require_user's LoginRequired) instead of returning 403 here. FastAPI
    # resolves Depends(...) params before the body runs, so a future author who turns
    # this into a dependency listed ahead of `user` would silently invert that: a
    # logged-out caller would see 403 before require_user ever gets a chance to run.
    sid = request.cookies.get(auth.COOKIE_NAME, "")
    if not sid or not auth.check_csrf(sid, token):
        raise HTTPException(status_code=403, detail="bad csrf token")


def page(
    request: Request, name: str, user: sqlite3.Row | None = None, no_store: bool = False, **ctx
):
    # A csrf token is minted whenever `user` is truthy -- page() takes that on faith and
    # does not itself verify that `user` came from a validated session; a hand-made dict
    # would work just as well. Callers MUST obtain `user` via Depends(require_user), which
    # is the only thing that actually ties it to a checked cookie. Every caller today does.
    sid = request.cookies.get(auth.COOKIE_NAME, "") if user else ""
    response = templates.TemplateResponse(
        request, name, {"user": user, "csrf": auth.csrf_token(sid) if sid else "", **ctx}
    )
    if no_store:
        response.headers["Cache-Control"] = "no-store"
    return response


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


def _expires_in(expires_at: int) -> str:
    seconds = max(0, expires_at - clock.now())
    days, rem = divmod(seconds, 86400)
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    hours, rem = divmod(rem, 3600)
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = rem // 60
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return "under a minute"


templates.env.filters["expires_in"] = _expires_in


@router.get("/")
def index(
    request: Request,
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    db.reap(conn)
    return page(
        request,
        "index.html",
        user=user,
        no_store=True,
        # Newest activity first: the human wants what moved, not what dies first.
        # The API keeps store.list_folders' expiry order.
        folders=sorted(store.list_folders(conn), key=lambda f: f["last_post_at"], reverse=True),
        ttl_days=request.app.state.ttl_days,
    )


@router.get("/f/{slug}")
def folder_page(
    slug: str,
    request: Request,
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    folder = store.get_folder(conn, slug)
    if folder is None:
        raise HTTPException(status_code=404, detail="no such folder")
    return page(
        request,
        "folder.html",
        user=user,
        no_store=True,
        folder=folder,
        posts=store.list_posts(conn, slug),
        statuses=store.STATUSES,
    )


@router.post("/f/{slug}/post")
def create_post(
    slug: str,
    request: Request,
    csrf: str = Form(default=""),
    body: str = Form(...),
    title: str = Form(default=""),
    format: str = Form(default="md"),
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_csrf(request, csrf)
    store.add_post(
        conn,
        slug,
        user["username"],
        "human",
        title or None,
        format,
        body,
        ttl_days=request.app.state.ttl_days,
    )
    return RedirectResponse(f"/f/{slug}", status_code=303)


@router.post("/f/{slug}/status")
def update_status(
    slug: str,
    request: Request,
    csrf: str = Form(default=""),
    status: str = Form(...),
    owner: str = Form(default=""),
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_csrf(request, csrf)
    store.set_status(conn, slug, status, owner or None)
    return RedirectResponse(f"/f/{slug}", status_code=303)


@router.get("/f/{slug}/blob/{blob_id}")
def blob(
    slug: str,
    blob_id: str,
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = store.get_blob(conn, slug, blob_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such image")
    return Response(
        content=row["bytes"],
        media_type=row["mime"],
        headers={
            "Content-Disposition": f'inline; filename="{row["filename"]}"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/agents")
def agents_page(
    request: Request,
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return page(
        request,
        "agents.html",
        user=user,
        agents=auth.list_agents(conn),
        new_token=None,
        new_name=None,
        error=None,
    )


@router.post("/agents")
def mint_agent(
    request: Request,
    csrf: str = Form(default=""),
    name: str = Form(...),
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_csrf(request, csrf)
    try:
        _, token = auth.mint_agent(conn, name)
    except ValueError as exc:
        return page(
            request,
            "agents.html",
            user=user,
            agents=auth.list_agents(conn),
            new_token=None,
            new_name=None,
            error=str(exc),
        )
    response = page(
        request,
        "agents.html",
        user=user,
        agents=auth.list_agents(conn),
        new_token=token,
        new_name=name,
        error=None,
    )
    # The plaintext token lives in this response body and nowhere else. no-store keeps
    # it out of disk caches and shared-machine history-replay a step short of what a
    # browser's own back/forward session history can still do (unavoidable for any
    # reveal-once secret rendered as HTML).
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/agents/{agent_id}/revoke")
def revoke_agent(
    agent_id: str,
    request: Request,
    csrf: str = Form(default=""),
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_csrf(request, csrf)
    auth.revoke_agent(conn, agent_id)
    return RedirectResponse("/agents", status_code=303)
