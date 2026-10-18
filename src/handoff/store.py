"""Folder, post, and blob operations. The only module that writes application rows."""

import json
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

# No unbounded client text reaches the database. Both front doors (agent JSON, human form)
# funnel through add_post/set_status, so capping here covers both -- same reasoning as the
# image caps above, just for the fields that used to have no ceiling at all.
#
# MAX_BODY_BYTES is measured in encoded utf-8 bytes, not code points: it exists as the real
# backstop for the byte-based Content-Length check in app.py's middleware (which a chunked
# request can skip), so a unit that varies with encoding would defeat the point. The others
# are character counts -- they bound display width, not storage, and a 200-character title
# is a 200-character title regardless of encoding.
MAX_BODY_BYTES = 1024 * 1024
MAX_TITLE_CHARS = 200
MAX_AUTHOR_NOTE_CHARS = 200
MAX_OWNER_CHARS = 100
MAX_FOLDER_TITLE_CHARS = 200

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
    return cleaned[:64].strip("-.") or "file"


def create_folder(conn: sqlite3.Connection, slug: str, title: str, ttl_days: int) -> sqlite3.Row:
    if not valid_slug(slug):
        raise Invalid(f"invalid slug: {slug!r}")
    if len(title) > MAX_FOLDER_TITLE_CHARS:
        raise Invalid(f"title exceeds {MAX_FOLDER_TITLE_CHARS} characters")
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
    row = get_folder(conn, slug)
    if row is None:
        # A reap can land between the commit above and this read-back (or, at
        # ttl_days=0, the row is already expired the instant it's written). Either
        # way, a folder that just can't be read back is a 404, not a None the
        # caller would crash on.
        raise NotFound(slug)
    return row


def get_folder(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM folders WHERE slug = ? AND expires_at > ?", (slug, clock.now())
    ).fetchone()


def list_folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        # Soonest to expire first: the board's job is to surface what runs out first.
        "SELECT * FROM folders WHERE expires_at > ? ORDER BY expires_at, slug",
        (clock.now(),),
    ).fetchall()


def list_posts(conn: sqlite3.Connection, slug: str, since: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE folder = ? AND id > ? ORDER BY id", (slug, since)
    ).fetchall()


def get_blob(conn: sqlite3.Connection, slug: str, blob_id: str) -> sqlite3.Row | None:
    # Joined against folders and filtered on expires_at, like get_folder and list_folders:
    # reads are what make expiry take effect instantly, not the reaper -- it's a backstop.
    return conn.execute(
        "SELECT blobs.* FROM blobs JOIN folders ON folders.slug = blobs.folder"
        " WHERE blobs.id = ? AND blobs.folder = ? AND folders.expires_at > ?",
        (blob_id, slug, clock.now()),
    ).fetchone()


def set_status(conn: sqlite3.Connection, slug: str, status: str, owner: str | None) -> None:
    if status not in STATUSES:
        raise Invalid(f"invalid status: {status!r}")
    if owner is not None and len(owner) > MAX_OWNER_CHARS:
        raise Invalid(f"owner exceeds {MAX_OWNER_CHARS} characters")
    # The expiry check is folded into the UPDATE itself, not a separate get_folder
    # call beforehand: a reap landing between the two would otherwise make this
    # UPDATE match zero rows and report success on a folder that's already gone.
    cur = conn.execute(
        "UPDATE folders SET status = ?, owner = ? WHERE slug = ? AND expires_at > ?",
        (status, owner, slug, clock.now()),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise NotFound(slug)


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
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise Invalid(f"body exceeds {MAX_BODY_BYTES} bytes")
    if title is not None and len(title) > MAX_TITLE_CHARS:
        raise Invalid(f"title exceeds {MAX_TITLE_CHARS} characters")
    if author_note is not None and len(author_note) > MAX_AUTHOR_NOTE_CHARS:
        raise Invalid(f"author_note exceeds {MAX_AUTHOR_NOTE_CHARS} characters")
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
        # Matched by exact error code, not message text: SQLite bundles differ across
        # the Python versions this project supports, and the code is stable since
        # sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY was exposed in Python 3.11, this
        # project's floor. Any other integrity error (e.g. a CHECK constraint) is a
        # real bug: let it propagate.
        if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY:
            raise NotFound(slug) from exc
        raise
    return post_id


# --- factory boards ------------------------------------------------------------------
#
# A factory run (busnes.app /factory) owns its board.json and job markdown files. It
# replaces the whole snapshot on every sync, so a write is idempotent and the server never
# merges state. Stage names come from the board itself: editing the loop needs no deploy.

VERDICTS = ("pending", "PASS", "CHANGES")
MAX_STAGES = 12
MAX_JOBS = 200
MAX_ROUNDS = 10
MAX_REVIEWERS = 8
MAX_FILES = 500
MAX_FILE_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

# <job-id>/<name>.md or <job-id>/round-<n>/<name>.md, the layout factory/jobs/ uses.
_FILE_RE = re.compile(
    r"^([a-z0-9][a-z0-9-]{0,63})/((?:round-[0-9]{1,2}/)?[a-z0-9][a-z0-9-]{0,63}\.md)$"
)


def sync_factory(
    conn: sqlite3.Connection,
    name: str,
    stages: list[str],
    jobs: list[dict],
    files: dict[str, str],
    ttl_days: int,
) -> None:
    if not valid_slug(name):
        raise Invalid(f"invalid factory name: {name!r}")
    if not stages or len(stages) > MAX_STAGES or len(set(stages)) != len(stages):
        raise Invalid(f"stages must be 1-{MAX_STAGES} distinct names")
    for stage in stages:
        if not valid_slug(stage):
            raise Invalid(f"invalid stage: {stage!r}")
    if len(jobs) > MAX_JOBS:
        raise Invalid(f"more than {MAX_JOBS} jobs")
    if len(files) > MAX_FILES:
        raise Invalid(f"more than {MAX_FILES} files")

    ids = set()
    for job in jobs:
        if not valid_slug(job["id"]) or job["id"] in ids:
            raise Invalid(f"invalid or duplicate job id: {job['id']!r}")
        ids.add(job["id"])
        if job["stage"] not in stages:
            raise Invalid(f"job {job['id']}: stage {job['stage']!r} is not in stages")
        for field in ("title", "repo", "branch"):
            if len(job[field]) > MAX_TITLE_CHARS:
                raise Invalid(f"job {job['id']}: {field} exceeds {MAX_TITLE_CHARS} characters")
        if len(job["reviews"]) > MAX_ROUNDS:
            raise Invalid(f"job {job['id']}: more than {MAX_ROUNDS} rounds")
        for chips in job["reviews"]:
            if len(chips) > MAX_REVIEWERS:
                raise Invalid(f"job {job['id']}: more than {MAX_REVIEWERS} reviewers")
            for reviewer, verdict in chips.items():
                if not valid_slug(reviewer) or verdict not in VERDICTS:
                    raise Invalid(f"job {job['id']}: bad review {reviewer!r}: {verdict!r}")

    rows = []
    total = 0
    for path, source in files.items():
        m = _FILE_RE.fullmatch(path)
        if m is None or m.group(1) not in ids:
            raise Invalid(f"invalid file path: {path!r}")
        size = len(source.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise Invalid(f"{path} exceeds {MAX_FILE_BYTES} bytes")
        total += size
        rows.append((name, m.group(1), m.group(2), render(source, "md", {})))
    if total > MAX_SNAPSHOT_BYTES:
        raise Invalid(f"files exceed {MAX_SNAPSHOT_BYTES} bytes")

    now = clock.now()
    board = json.dumps({"stages": stages, "jobs": jobs})
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO factories (name, board, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (name, board, now, now + ttl_days * 86400),
        )
        conn.execute("DELETE FROM factory_files WHERE factory = ?", (name,))
        conn.executemany("INSERT INTO factory_files VALUES (?, ?, ?, ?)", rows)


def list_factories(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM factories WHERE expires_at > ? ORDER BY updated_at DESC, name",
        (clock.now(),),
    ).fetchall()
    return [
        {"name": r["name"], "updated_at": r["updated_at"], **json.loads(r["board"])} for r in rows
    ]


def get_factory_job(conn: sqlite3.Connection, name: str, job_id: str) -> tuple[dict, list] | None:
    """The job's board entry and its rendered files, spec first and decision last."""
    factory = next((f for f in list_factories(conn) if f["name"] == name), None)
    job = factory and next((j for j in factory["jobs"] if j["id"] == job_id), None)
    if job is None:
        return None
    files = conn.execute(
        "SELECT path, html FROM factory_files WHERE factory = ? AND job = ?", (name, job_id)
    ).fetchall()
    order = {"spec.md": 0, "build.md": 1, "decision.md": 3}

    def key(f: sqlite3.Row) -> tuple:
        # round-10 after round-9, not after round-1.
        return order.get(f["path"], 2), re.sub(r"\d+", lambda m: m[0].zfill(3), f["path"])

    return job, sorted(files, key=key)
