"""Folder, post, and blob operations. The only module that writes application rows."""

import re
import secrets
import sqlite3
from collections.abc import Sequence

from handoff import clock
from handoff.render import render

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
STATUSES = ("open", "claimed", "blocked", "done")
FORMATS = ("md", "html", "text")

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class NotFound(Exception):
    """The folder or blob does not exist, or has expired."""


class Invalid(Exception):
    """The request is malformed. Maps to HTTP 400."""


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug))


def sniff_mime(data: bytes) -> str | None:
    """Identify an image from its magic bytes. Returns None for anything not allowed."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_filename(name: str) -> str:
    """Reduce a client-supplied name to a bare, substitution-safe filename.

    Idempotent: truncating before stripping means a stray hyphen exposed by
    the cut is removed, not left for a second pass to find.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base)
    return cleaned[:64].strip("-") or "file"


def create_folder(conn: sqlite3.Connection, slug: str, title: str, ttl_days: int) -> sqlite3.Row:
    if not valid_slug(slug):
        raise Invalid(f"invalid slug: {slug!r}")
    existing = get_folder(conn, slug)
    if existing is not None:
        return existing

    now = clock.now()
    conn.execute(
        "INSERT OR REPLACE INTO folders (slug, title, status, owner, created_at,"
        " last_post_at, expires_at) VALUES (?, ?, 'open', NULL, ?, ?, ?)",
        (slug, title, now, now, now + ttl_days * 86400),
    )
    conn.commit()
    return get_folder(conn, slug)


def get_folder(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM folders WHERE slug = ? AND expires_at > ?", (slug, clock.now())
    ).fetchone()


def list_folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM folders WHERE expires_at > ? ORDER BY slug", (clock.now(),)
    ).fetchall()


def list_posts(conn: sqlite3.Connection, slug: str, since: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE folder = ? AND id > ? ORDER BY id", (slug, since)
    ).fetchall()


def get_blob(conn: sqlite3.Connection, slug: str, blob_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM blobs WHERE id = ? AND folder = ?", (blob_id, slug)
    ).fetchone()


def set_status(conn: sqlite3.Connection, slug: str, status: str, owner: str | None) -> None:
    if status not in STATUSES:
        raise Invalid(f"invalid status: {status!r}")
    if get_folder(conn, slug) is None:
        raise NotFound(slug)
    conn.execute("UPDATE folders SET status = ?, owner = ? WHERE slug = ?", (status, owner, slug))
    conn.commit()


def add_post(
    conn: sqlite3.Connection,
    slug: str,
    author: str,
    author_kind: str,
    title: str | None,
    fmt: str,
    body: str,
    author_note: str | None = None,
    images: Sequence[tuple[str, bytes]] = (),
    ttl_days: int = 7,
) -> int:
    if fmt not in FORMATS:
        raise Invalid(f"invalid format: {fmt!r}")
    if get_folder(conn, slug) is None:
        raise NotFound(slug)

    # Blob ids are random, so they can be minted before insert. That means the URLs are
    # known at render time and the post row is written complete, in one transaction.
    total_bytes = sum(len(data) for _, data in images)
    if total_bytes > MAX_TOTAL_IMAGE_BYTES:
        raise Invalid(f"total image size exceeds {MAX_TOTAL_IMAGE_BYTES} bytes")

    prepared = []
    blob_urls = {}
    for raw_name, data in images:
        if len(data) > MAX_IMAGE_BYTES:
            raise Invalid(f"image too large: {raw_name}")
        mime = sniff_mime(data)
        if mime is None:
            raise Invalid(f"not an allowed image type: {raw_name}")
        name = safe_filename(raw_name)
        if name in blob_urls or raw_name in blob_urls:
            raise Invalid(f"duplicate image filename: {raw_name}")
        blob_id = secrets.token_hex(16)
        prepared.append((blob_id, name, mime, data))
        blob_urls[name] = f"/f/{slug}/blob/{blob_id}"
        blob_urls[raw_name] = f"/f/{slug}/blob/{blob_id}"

    html = render(body, fmt, blob_urls)
    now = clock.now()

    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO posts (folder, author, author_kind, author_note, title,"
                " source_format, source, html, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, author, author_kind, author_note, title, fmt, body, html, now),
            )
            post_id = cur.lastrowid
            for blob_id, name, mime, data in prepared:
                conn.execute(
                    "INSERT INTO blobs (id, folder, post_id, filename, mime, bytes, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (blob_id, slug, post_id, name, mime, data, now),
                )
            conn.execute(
                "UPDATE folders SET last_post_at = ?, expires_at = ? WHERE slug = ?",
                (now, now + ttl_days * 86400, slug),
            )
    except sqlite3.IntegrityError as exc:
        # The folder's TTL can lapse and be reaped by another connection between the
        # existence check above and this INSERT. That race surfaces here as a foreign
        # key violation, which is really just a slower NotFound -- not a server error.
        # Any other integrity error (e.g. a CHECK constraint) is a real bug: let it propagate.
        if "FOREIGN KEY" in str(exc):
            raise NotFound(slug) from exc
        raise
    return post_id
