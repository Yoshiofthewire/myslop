"""Agent-facing JSON API. Bearer token only -- cookies are never consulted."""

import base64
import binascii
import sqlite3
from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from handoff import auth, clock, db, store
from handoff.app import get_conn

router = APIRouter(prefix="/api")

RATE_LIMIT = 60  # requests per minute per token
_hits: dict[str, list[int]] = defaultdict(list)


def require_agent(
    request: Request,
    authorization: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    agent = auth.agent_by_token(conn, token)
    if agent is None:
        raise HTTPException(status_code=401, detail="unknown or revoked token")

    now = clock.now()
    recent = [t for t in _hits[agent["id"]] if t > now - 60]
    if len(recent) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    recent.append(now)
    _hits[agent["id"]] = recent
    return agent


class NewFolder(BaseModel):
    slug: str
    title: str


class Image(BaseModel):
    filename: str
    content_b64: str


class NewPost(BaseModel):
    title: str | None = None
    format: str = "md"
    body: str
    author_note: str | None = None
    images: list[Image] = Field(default_factory=list)
    status: str | None = None
    owner: str | None = None


class StatusUpdate(BaseModel):
    status: str
    owner: str | None = None


def _folder_json(row: sqlite3.Row) -> dict:
    return {
        "slug": row["slug"],
        "title": row["title"],
        "status": row["status"],
        "owner": row["owner"],
        "created_at": row["created_at"],
        "last_post_at": row["last_post_at"],
        "expires_at": row["expires_at"],
    }


def _post_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "author": row["author"],
        "author_kind": row["author_kind"],
        "author_note": row["author_note"],
        "title": row["title"],
        "format": row["source_format"],
        "body": row["source"],
        "created_at": row["created_at"],
    }


@router.post("/folders")
def create_folder(
    body: NewFolder,
    request: Request,
    agent: sqlite3.Row = Depends(require_agent),
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = store.create_folder(conn, body.slug, body.title, request.app.state.ttl_days)
    return _folder_json(row)


@router.get("/folders")
def list_folders(
    agent: sqlite3.Row = Depends(require_agent),
    conn: sqlite3.Connection = Depends(get_conn),
):
    db.reap(conn)
    return [_folder_json(r) for r in store.list_folders(conn)]


@router.get("/folders/{slug}")
def get_folder(
    slug: str,
    since: int = 0,
    agent: sqlite3.Row = Depends(require_agent),
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = store.get_folder(conn, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="no such folder")
    out = _folder_json(row)
    out["posts"] = [_post_json(p) for p in store.list_posts(conn, slug, since)]
    return out


@router.post("/folders/{slug}/posts")
def create_post(
    slug: str,
    body: NewPost,
    request: Request,
    agent: sqlite3.Row = Depends(require_agent),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if body.status is not None and body.status not in store.STATUSES:
        raise store.Invalid(f"invalid status: {body.status!r}")
    if body.owner is not None and len(body.owner) > store.MAX_OWNER_CHARS:
        raise store.Invalid(f"owner exceeds {store.MAX_OWNER_CHARS} characters")

    images = []
    for img in body.images:
        try:
            data = base64.b64decode(img.content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise store.Invalid(f"bad base64 for {img.filename}") from exc
        images.append((img.filename, data))

    post_id = store.add_post(
        conn,
        slug,
        agent["name"],
        "agent",
        body.title,
        body.format,
        body.body,
        author_note=body.author_note,
        images=images,
        ttl_days=request.app.state.ttl_days,
    )
    if body.status is not None:
        store.set_status(conn, slug, body.status, body.owner)

    blobs = conn.execute("SELECT id, filename FROM blobs WHERE post_id = ?", (post_id,)).fetchall()
    return {
        "id": post_id,
        "images": [{"filename": b["filename"], "url": f"/f/{slug}/blob/{b['id']}"} for b in blobs],
    }


@router.post("/folders/{slug}/status")
def set_status(
    slug: str,
    body: StatusUpdate,
    agent: sqlite3.Row = Depends(require_agent),
    conn: sqlite3.Connection = Depends(get_conn),
):
    store.set_status(conn, slug, body.status, body.owner)
    row = store.get_folder(conn, slug)
    if row is None:
        # A reap can land between set_status's commit and this read-back.
        raise store.NotFound(slug)
    return _folder_json(row)
