# Agent Hand-off Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted service where AI agents on different PCs hand work off to each other and to a human, with everything expiring seven days after the last post.

**Architecture:** One FastAPI process over one SQLite file. Two front doors — a JSON API authenticated by bearer UUID for agents, and server-rendered HTML authenticated by session cookie for the human — sharing a single store layer. Agent-supplied Markdown and HTML pass through exactly one sanitizer before storage, and the pages that display it forbid JavaScript entirely.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, SQLite (stdlib `sqlite3`), markdown-it-py, nh3, argon2-cffi, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-26-agent-handoff-site-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **Python 3.11 or later.** Union syntax `X | None` is used throughout.
- **Runtime dependencies are exactly:** `fastapi`, `uvicorn[standard]`, `jinja2`, `markdown-it-py`, `nh3`, `argon2-cffi`, `python-multipart`. Adding any other runtime dependency requires justification in the commit message.
- **No site JavaScript, ever.** The CSP sends `script-src 'none'`. No `<script>` tags, no inline handlers, no `onclick` in templates. This is a permanent constraint, not a v1 shortcut.
- **No inline `style` attributes or `<style>` blocks in templates.** CSP sends `style-src 'self'`; all styling lives in `src/handoff/static/style.css`.
- **All route handlers are sync `def`, never `async def`.** `sqlite3` is blocking; sync handlers let FastAPI run them in its threadpool. An `async def` handler would block the event loop.
- **Time comes only from `clock.now()`.** Import the module (`from handoff import clock`) and call `clock.now()`. Never `from handoff.clock import now` — that binds the function at import time and defeats test monkeypatching. Never call `time.time()` outside `clock.py`.
- **Never trust the client for:** post author, image MIME type, image filename, or folder existence. Each is derived or validated server-side.
- **Only `render.render()` may produce a value stored in `posts.html`.** No other code path writes that column.
- **The service never binds `0.0.0.0`.** Default bind is `127.0.0.1`.
- **The session cookie is always `HttpOnly` and `SameSite=Lax`; its `Secure` flag is derived from `request.url.scheme == "https"`, never hardcoded.** Verified behaviour: a `Secure` cookie set over plain http is never sent back, so hardcoding it makes login impossible on the documented plain-http tailnet deployment while protecting nothing (tailnet traffic is already WireGuard-encrypted). `uvicorn` runs with `proxy_headers=True` so a TLS-terminating proxy still yields `Secure`.
- **Slug format:** `^[a-z0-9][a-z0-9-]{0,63}$`, exact.
- **Statuses:** exactly `open`, `claimed`, `blocked`, `done`.
- **Default TTL:** 7 days, from `HANDOFF_TTL_DAYS`, threaded explicitly as a `ttl_days: int` parameter — never read from the environment below the app factory.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, ruff and pytest config |
| `src/handoff/clock.py` | The single source of current time; the seam tests inject through |
| `src/handoff/db.py` | Connection + pragmas, schema, the expiry reaper |
| `src/handoff/render.py` | Markdown → HTML, sanitization, `img:` resolution. Sole writer of stored HTML |
| `src/handoff/auth.py` | Token hashing, password hashing, sessions, CSRF, login throttle |
| `src/handoff/store.py` | Folder/post/blob operations, slug validation, MIME sniffing |
| `src/handoff/api.py` | Agent JSON routes + bearer dependency + rate limit |
| `src/handoff/web.py` | Human HTML routes + cookie dependency |
| `src/handoff/app.py` | App factory, CSP middleware, per-request connection |
| `src/handoff/cli.py` | `createuser`, `reap`, `serve` |
| `src/handoff/templates/` | `base.html`, `index.html`, `folder.html`, `login.html`, `agents.html` |
| `src/handoff/static/style.css` | All styling |
| `tests/` | One test module per source module, plus `conftest.py` |
| `skills/handoff/SKILL.md` | The skill agents install |
| `Dockerfile`, `handoff.service`, `README.md` | Deployment |
| `.github/workflows/ci.yml` | ruff + pytest |

---

### Task 1: Project scaffolding, clock, schema, and the reaper

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/handoff/__init__.py`, `src/handoff/clock.py`, `src/handoff/db.py`, `.github/workflows/ci.yml`
- Test: `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `clock.now() -> int` — current Unix seconds.
  - `db.connect(path: str) -> sqlite3.Connection` — pragmas set, `row_factory = sqlite3.Row`.
  - `db.init_schema(conn: sqlite3.Connection) -> None`
  - `db.reap(conn: sqlite3.Connection) -> int` — deletes expired folders, returns the count.
  - `db.SCHEMA: str`
  - pytest fixture `conn` — an initialized in-memory database.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "handoff"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "jinja2",
    "markdown-it-py",
    "nh3",
    "argon2-cffi",
    "python-multipart",
]

[project.optional-dependencies]
dev = ["pytest", "httpx", "ruff"]

[project.scripts]
handoff = "handoff.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
handoff = ["templates/*.html", "static/*.css"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "S"]
ignore = ["S101"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S105", "S106"]
```

- [ ] **Step 2: Create `.gitignore`**

```
__pycache__/
*.egg-info/
.venv/
.pytest_cache/
.ruff_cache/
*.db
*.db-wal
*.db-shm
```

- [ ] **Step 3: Create the virtualenv and install**

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

- [ ] **Step 4: Write `src/handoff/__init__.py` and `src/handoff/clock.py`**

`src/handoff/__init__.py` is empty.

```python
"""The single source of current time, so tests can control it."""

import time


def now() -> int:
    """Current Unix time in whole seconds."""
    return int(time.time())
```

- [ ] **Step 5: Write the failing tests for `db.py`**

`tests/conftest.py`:

```python
import pytest

from handoff import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()
```

`tests/test_db.py`:

```python
import sqlite3

import pytest

from handoff import clock, db

DAY = 86400


def _folder(conn, slug, expires_at):
    conn.execute(
        "INSERT INTO folders (slug, title, created_at, last_post_at, expires_at)"
        " VALUES (?, ?, 0, 0, ?)",
        (slug, slug, expires_at),
    )
    conn.commit()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO posts (folder, author, author_kind, source_format,"
            " source, html, created_at) VALUES ('nope', 'a', 'agent', 'md', '', '', 0)"
        )


def test_status_check_constraint_rejects_garbage(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO folders (slug, title, status, created_at, last_post_at,"
            " expires_at) VALUES ('a', 'a', 'sideways', 0, 0, 0)"
        )


def test_reap_deletes_only_expired_folders(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 10 * DAY)
    _folder(conn, "old", 9 * DAY)
    _folder(conn, "fresh", 11 * DAY)

    assert db.reap(conn) == 1

    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM folders")]
    assert slugs == ["fresh"]


def test_reap_cascades_to_posts_and_blobs(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 10 * DAY)
    _folder(conn, "old", 9 * DAY)
    conn.execute(
        "INSERT INTO posts (id, folder, author, author_kind, source_format,"
        " source, html, created_at) VALUES (1, 'old', 'a', 'agent', 'md', '', '', 0)"
    )
    conn.execute(
        "INSERT INTO blobs (id, folder, post_id, filename, mime, bytes, created_at)"
        " VALUES ('b1', 'old', 1, 'x.png', 'image/png', X'00', 0)"
    )
    conn.commit()

    db.reap(conn)

    assert conn.execute("SELECT count(*) c FROM posts").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM blobs").fetchone()["c"] == 0


def test_reap_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 10 * DAY)
    _folder(conn, "old", 9 * DAY)

    assert db.reap(conn) == 1
    assert db.reap(conn) == 0
    assert db.reap(conn) == 0


def test_init_schema_runs_twice_without_error(conn):
    db.init_schema(conn)
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError` or `AttributeError: module 'handoff.db' has no attribute 'connect'`.

- [ ] **Step 7: Write `src/handoff/db.py`**

`auto_vacuum` must be set before any table exists, so `connect()` sets it and `init_schema()` runs afterwards. `IF NOT EXISTS` everywhere makes `init_schema` idempotent, which is what lets the app call it unconditionally at startup.

```python
"""Connection setup, schema, and the expiry reaper."""

import sqlite3

from handoff import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  token_hash  BLOB NOT NULL UNIQUE,
  created_at  INTEGER NOT NULL,
  revoked_at  INTEGER
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  oidc_sub      TEXT UNIQUE,
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
  slug         TEXT PRIMARY KEY,
  title        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','claimed','blocked','done')),
  owner        TEXT,
  created_at   INTEGER NOT NULL,
  last_post_at INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS folders_expires ON folders(expires_at);

CREATE TABLE IF NOT EXISTS posts (
  id            INTEGER PRIMARY KEY,
  folder        TEXT NOT NULL REFERENCES folders(slug) ON DELETE CASCADE,
  author        TEXT NOT NULL,
  author_kind   TEXT NOT NULL CHECK (author_kind IN ('agent','human')),
  author_note   TEXT,
  title         TEXT,
  source_format TEXT NOT NULL CHECK (source_format IN ('md','html','text')),
  source        TEXT NOT NULL,
  html          TEXT NOT NULL,
  created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_folder_id ON posts(folder, id);

CREATE TABLE IF NOT EXISTS blobs (
  id         TEXT PRIMARY KEY,
  folder     TEXT NOT NULL REFERENCES folders(slug) ON DELETE CASCADE,
  post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  filename   TEXT NOT NULL,
  mime       TEXT NOT NULL,
  bytes      BLOB NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas this service depends on."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reap(conn: sqlite3.Connection) -> int:
    """Delete folders whose sliding TTL has run out. Safe to call any number of times."""
    cur = conn.execute("DELETE FROM folders WHERE expires_at <= ?", (clock.now(),))
    conn.commit()
    if cur.rowcount:
        conn.execute("PRAGMA incremental_vacuum")
        conn.commit()
    return cur.rowcount
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 9: Add CI**

`.github/workflows/ci.yml`:

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: ruff format --check .
      - run: pytest -v
```

- [ ] **Step 10: Run ruff and fix anything it reports**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml .gitignore .github src tests
git commit -m "feat: schema, connection pragmas, and idempotent expiry reaper"
```

---

### Task 2: Rendering and sanitization

**Files:**
- Create: `src/handoff/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `render.render(source: str, fmt: str, blob_urls: dict[str, str]) -> str` — `fmt` is `"md"`, `"html"`, or `"text"`; `blob_urls` maps an uploaded filename to its same-origin URL. Raises `ValueError` on an unknown `fmt`.
  - `render.ALLOWED_TAGS: set[str]`, `render.ALLOWED_ATTRS: dict[str, set[str]]`

`img:` references are resolved **on the source text, before Markdown rendering**. Filenames are constrained to `[A-Za-z0-9._-]+` at upload (Task 4), so the substitution target is a literal string and the replacement is a same-origin path. Any `img:` reference with no matching upload survives into the HTML and is then dropped by the sanitizer, because `img` is not an allowed URL scheme.

- [ ] **Step 1: Write the failing tests**

`tests/test_render.py`:

```python
import pytest

from handoff.render import render

# Each payload must not survive rendering. The assertion is on the absence of the
# executable part, not on an exact output string, so a sanitizer upgrade that changes
# formatting does not produce a false failure.
XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<a href=\"javascript:alert(1)\">click</a>",
    "<svg onload=alert(1)></svg>",
    "<iframe src=\"data:text/html,<script>alert(1)</script>\"></iframe>",
    "<div style=\"background:url(javascript:alert(1))\">x</div>",
    "<object data=\"data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==\"></object>",
    "<form action=\"http://evil\"><input name=x></form>",
    "<a href=\"vbscript:alert(1)\">x</a>",
    "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
    "<style>@import 'http://evil/x.css';</style>",
    "<base href=\"http://evil/\">",
    "<a href=\"&#106;avascript:alert(1)\">x</a>",
]

FORBIDDEN = ["<script", "onerror", "onload", "javascript:", "vbscript:", "<iframe",
             "<object", "<form", "<style", "<base", "srcdoc"]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
@pytest.mark.parametrize("fmt", ["md", "html"])
def test_xss_payloads_do_not_survive(payload, fmt):
    out = render(payload, fmt, {}).lower()
    for needle in FORBIDDEN:
        assert needle not in out, f"{needle!r} survived {fmt} rendering of {payload!r}"


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_text_format_escapes_every_payload(payload):
    # `text` is escaped, not sanitized, so the FORBIDDEN substring check does not apply --
    # "onerror" legitimately survives as the literal text "onerror". The property that
    # matters is that no markup survives at all.
    out = render(payload, "text", {})
    assert out.startswith("<pre>") and out.endswith("</pre>")
    inner = out[len("<pre>") : -len("</pre>")]
    assert "<" not in inner
    assert ">" not in inner


def test_markdown_basics_render():
    out = render("# Title\n\n- a\n- b\n", "md", {})
    assert "<h1>Title</h1>" in out
    assert "<li>a</li>" in out


def test_markdown_tables_are_enabled():
    out = render("| a | b |\n| - | - |\n| 1 | 2 |\n", "md", {})
    assert "<table>" in out
    assert "<td>1</td>" in out


def test_text_format_is_escaped_and_preformatted():
    out = render("<b>not bold</b>", "text", {})
    assert out.startswith("<pre>")
    assert "&lt;b&gt;" in out
    assert "<b>" not in out


def test_safe_html_passes_through():
    out = render("<p>hello <strong>there</strong></p>", "html", {})
    assert "<strong>there</strong>" in out


def test_links_get_rel_and_keep_https():
    out = render("[x](https://example.com)", "md", {})
    assert 'href="https://example.com"' in out
    assert "noopener" in out
    assert "nofollow" in out


def test_img_reference_is_resolved_to_blob_url():
    out = render("![arch](img:arch.png)", "md", {"arch.png": "/f/s/blob/abc123"})
    assert 'src="/f/s/blob/abc123"' in out
    assert "img:" not in out


def test_unresolved_img_reference_is_dropped():
    out = render("![missing](img:nope.png)", "md", {})
    assert "img:nope.png" not in out


def test_img_resolution_does_not_apply_to_arbitrary_text():
    out = render("the string img:arch.png is not an image here", "text", {"arch.png": "/f/s/blob/x"})
    assert "/f/s/blob/x" not in out


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        render("x", "pdf", {})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff.render'`.

- [ ] **Step 3: Write `src/handoff/render.py`**

Note `test_img_resolution_does_not_apply_to_arbitrary_text`: resolution runs only for `md` and `html`, never for `text`, and only inside a `(img:...)` or `"img:..."` position. The implementation substitutes the exact token `img:<filename>` only when it is immediately preceded by `(`, `"`, or `'` — the three positions a URL can occupy in Markdown and HTML.

```python
"""Markdown rendering and the single sanitization choke point.

Nothing outside this module may produce a value stored in ``posts.html``.
"""

import html as html_escape
import re

import nh3
from markdown_it import MarkdownIt

ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "a", "img", "details", "summary",
}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

URL_SCHEMES = {"http", "https", "mailto"}

_md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

_FORMATS = ("md", "html", "text")


def _resolve_images(source: str, blob_urls: dict[str, str]) -> str:
    """Replace ``img:<filename>`` in URL position with the stored blob URL."""
    if not blob_urls:
        return source
    for filename, url in blob_urls.items():
        source = re.sub(
            r"(?<=[(\"'])img:" + re.escape(filename) + r"(?=[)\"'\s])",
            url,
            source,
        )
    return source


def sanitize(dirty: str) -> str:
    return nh3.clean(
        dirty,
        tags=ALLOWED_TAGS,
        attributes={k: set(v) for k, v in ALLOWED_ATTRS.items()},
        url_schemes=URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def render(source: str, fmt: str, blob_urls: dict[str, str]) -> str:
    """Turn a submitted body into HTML that is safe to embed in a page."""
    if fmt not in _FORMATS:
        raise ValueError(f"unknown format: {fmt!r}")

    if fmt == "text":
        return "<pre>" + html_escape.escape(source) + "</pre>"

    resolved = _resolve_images(source, blob_urls)
    dirty = _md.render(resolved) if fmt == "md" else resolved
    return sanitize(dirty)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_render.py -v`
Expected: PASS, 51 tests (14 payloads × 2 sanitized formats, 14 escaped-text payloads, plus 9 behavioural tests).

If `test_links_get_rel_and_keep_https` fails on the exact `rel` string, read the installed nh3's `link_rel` behaviour with `python -c "import nh3; help(nh3.clean)"` and adjust the assertion to match observed output — do not weaken the XSS assertions to make anything pass.

- [ ] **Step 5: Commit**

```bash
git add src/handoff/render.py tests/test_render.py
git commit -m "feat: markdown rendering with a single sanitization choke point"
```

---

### Task 3: Authentication primitives

**Files:**
- Create: `src/handoff/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `db.connect`, `db.init_schema`, `clock.now`.
- Produces:
  - `auth.hash_token(token: str) -> bytes`
  - `auth.mint_agent(conn, name: str) -> tuple[str, str]` — returns `(agent_id, plaintext_uuid)`.
  - `auth.agent_by_token(conn, token: str) -> sqlite3.Row | None` — `None` if unknown or revoked.
  - `auth.revoke_agent(conn, agent_id: str) -> None`
  - `auth.list_agents(conn) -> list[sqlite3.Row]`
  - `auth.create_user(conn, username: str, password: str) -> int`
  - `auth.verify_user(conn, username: str, password: str) -> sqlite3.Row | None`
  - `auth.create_session(conn, user_id: int) -> str`
  - `auth.session_user(conn, sid: str) -> sqlite3.Row | None`
  - `auth.delete_session(conn, sid: str) -> None`
  - `auth.csrf_token(sid: str) -> str`
  - `auth.check_csrf(sid: str, token: str) -> bool`
  - `auth.SESSION_TTL: int`, `auth.COOKIE_NAME: str`

CSRF tokens are an HMAC keyed by the session id itself. The session id is secret and `HttpOnly`, so the derived token is unguessable to an attacker without needing a separate server secret to manage.

- [ ] **Step 1: Write the failing tests**

`tests/test_auth.py`:

```python
import pytest

from handoff import auth, clock


def test_mint_agent_returns_a_usable_token(conn):
    agent_id, token = auth.mint_agent(conn, "opus-desktop")

    row = auth.agent_by_token(conn, token)
    assert row is not None
    assert row["id"] == agent_id
    assert row["name"] == "opus-desktop"


def test_plaintext_token_is_never_stored(conn):
    _, token = auth.mint_agent(conn, "opus-desktop")

    stored = conn.execute("SELECT token_hash FROM agents").fetchone()["token_hash"]
    assert token.encode() not in stored
    assert stored == auth.hash_token(token)


def test_unknown_token_is_rejected(conn):
    auth.mint_agent(conn, "opus-desktop")
    assert auth.agent_by_token(conn, "00000000-0000-0000-0000-000000000000") is None


def test_revoked_token_is_rejected(conn):
    agent_id, token = auth.mint_agent(conn, "opus-desktop")
    auth.revoke_agent(conn, agent_id)
    assert auth.agent_by_token(conn, token) is None


def test_duplicate_agent_name_is_rejected(conn):
    auth.mint_agent(conn, "opus-desktop")
    with pytest.raises(ValueError):
        auth.mint_agent(conn, "opus-desktop")


def test_password_round_trip(conn):
    auth.create_user(conn, "yoshi", "correct horse battery staple")

    assert auth.verify_user(conn, "yoshi", "correct horse battery staple") is not None
    assert auth.verify_user(conn, "yoshi", "wrong") is None
    assert auth.verify_user(conn, "nobody", "correct horse battery staple") is None


def test_password_is_hashed_with_argon2id(conn):
    auth.create_user(conn, "yoshi", "hunter2")
    stored = conn.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
    assert stored.startswith("$argon2id$")
    assert "hunter2" not in stored


def test_repeated_failures_throttle_then_recover(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    auth.create_user(conn, "yoshi", "hunter2")
    auth.reset_throttle()

    for _ in range(5):
        assert auth.verify_user(conn, "yoshi", "wrong") is None

    # Correct password is refused while throttled.
    assert auth.verify_user(conn, "yoshi", "hunter2") is None

    t[0] += 3600
    assert auth.verify_user(conn, "yoshi", "hunter2") is not None


def test_session_round_trip(conn):
    uid = auth.create_user(conn, "yoshi", "hunter2")
    sid = auth.create_session(conn, uid)

    user = auth.session_user(conn, sid)
    assert user is not None
    assert user["username"] == "yoshi"

    auth.delete_session(conn, sid)
    assert auth.session_user(conn, sid) is None


def test_expired_session_is_rejected(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    uid = auth.create_user(conn, "yoshi", "hunter2")
    sid = auth.create_session(conn, uid)

    t[0] += auth.SESSION_TTL + 1
    assert auth.session_user(conn, sid) is None


def test_csrf_token_is_bound_to_the_session(conn):
    a = auth.csrf_token("session-a")
    b = auth.csrf_token("session-b")

    assert a != b
    assert auth.check_csrf("session-a", a)
    assert not auth.check_csrf("session-a", b)
    assert not auth.check_csrf("session-a", "")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff.auth'`.

- [ ] **Step 3: Write `src/handoff/auth.py`**

```python
"""Credentials, sessions, and CSRF.

All credential verification lives here. Adding OIDC later means adding a function
to this module and two routes -- nothing above it changes.
"""

import hashlib
import hmac
import secrets
import sqlite3
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from handoff import clock

SESSION_TTL = 30 * 86400
COOKIE_NAME = "handoff_session"

_ph = PasswordHasher()

# username -> (consecutive failures, unix time when attempts may resume)
_throttle: dict[str, tuple[int, int]] = {}
_THROTTLE_AFTER = 5
_THROTTLE_BASE = 2


def reset_throttle() -> None:
    """Clear login throttling. For tests and the CLI."""
    _throttle.clear()


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def mint_agent(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Create an agent and return (agent_id, plaintext token). The token is not recoverable."""
    agent_id = uuid.uuid4().hex
    token = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO agents (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, name, hash_token(token), clock.now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"agent name already in use: {name}") from exc
    conn.commit()
    return agent_id, token


def agent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    digest = hash_token(token)
    row = conn.execute(
        "SELECT * FROM agents WHERE token_hash = ? AND revoked_at IS NULL", (digest,)
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None
    return row


def revoke_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute("UPDATE agents SET revoked_at = ? WHERE id = ?", (clock.now(), agent_id))
    conn.commit()


def list_agents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, _ph.hash(password), clock.now()),
    )
    conn.commit()
    return cur.lastrowid


def verify_user(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    failures, resume_at = _throttle.get(username, (0, 0))
    if clock.now() < resume_at:
        return None

    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    ok = False
    if row is not None and row["password_hash"]:
        try:
            ok = _ph.verify(row["password_hash"], password)
        except VerifyMismatchError:
            ok = False

    if not ok:
        failures += 1
        delay = 0 if failures < _THROTTLE_AFTER else _THROTTLE_BASE ** (failures - _THROTTLE_AFTER + 1)
        _throttle[username] = (failures, clock.now() + delay)
        return None

    _throttle.pop(username, None)
    return row


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    sid = secrets.token_urlsafe(32)
    now = clock.now()
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (sid, user_id, now, now + SESSION_TTL),
    )
    conn.commit()
    return sid


def session_user(conn: sqlite3.Connection, sid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id"
        " WHERE sessions.id = ? AND sessions.expires_at > ?",
        (sid, clock.now()),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()


def csrf_token(sid: str) -> str:
    return hmac.new(sid.encode(), b"csrf", hashlib.sha256).hexdigest()


def check_csrf(sid: str, token: str) -> bool:
    return hmac.compare_digest(csrf_token(sid), token or "")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add src/handoff/auth.py tests/test_auth.py
git commit -m "feat: agent tokens, password sessions, CSRF, and login throttling"
```

---

### Task 4: The store layer

**Files:**
- Create: `src/handoff/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `clock.now`, `render.render`.
- Produces:
  - `store.SLUG_RE: re.Pattern`, `store.STATUSES: tuple[str, ...]`
  - `store.valid_slug(slug: str) -> bool`
  - `store.sniff_mime(data: bytes) -> str | None`
  - `store.safe_filename(name: str) -> str`
  - `store.create_folder(conn, slug: str, title: str, ttl_days: int) -> sqlite3.Row`
  - `store.get_folder(conn, slug: str) -> sqlite3.Row | None` — live folders only.
  - `store.list_folders(conn) -> list[sqlite3.Row]` — live folders only.
  - `store.list_posts(conn, slug: str, since: int = 0) -> list[sqlite3.Row]`
  - `store.add_post(conn, slug, author, author_kind, title, fmt, body, author_note=None, images=(), ttl_days=7) -> int` — `images` is a sequence of `(filename, data_bytes)`. Returns the new post id.
  - `store.set_status(conn, slug, status: str, owner: str | None) -> None`
  - `store.get_blob(conn, slug: str, blob_id: str) -> sqlite3.Row | None`
  - Exceptions: `store.NotFound`, `store.Invalid`

`add_post` generates blob ids first so the blob URLs are known before rendering, letting the post row be inserted complete in one transaction.

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
import pytest

from handoff import clock, store

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


@pytest.mark.parametrize("slug", ["a", "myslop-pr-42", "kypost-tls-migration", "x" * 64])
def test_valid_slugs_accepted(slug):
    assert store.valid_slug(slug)


@pytest.mark.parametrize(
    "slug",
    ["", "-leading", "UPPER", "has space", "has_underscore", "../etc/passwd",
     "/absolute", "a/b", "x" * 65, "café", "a.b", "dot."],
)
def test_invalid_slugs_rejected(slug):
    assert not store.valid_slug(slug)


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_sniff_recognises_allowed_images(data, expected):
    assert store.sniff_mime(data) == expected


@pytest.mark.parametrize("data", [b"<html>hi</html>", b"", b"%PDF-1.4", b"MZ\x90\x00"])
def test_sniff_rejects_everything_else(data):
    assert store.sniff_mime(data) is None


def test_safe_filename_strips_paths_and_exotic_characters():
    assert store.safe_filename("../../etc/passwd") == "passwd"
    assert store.safe_filename("a b;c.png") == "a-b-c.png"
    assert store.safe_filename("") == "file"


def test_create_folder_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 1000)
    first = store.create_folder(conn, "myslop-pr-42", "PR 42", 7)
    second = store.create_folder(conn, "myslop-pr-42", "different title", 7)

    assert second["slug"] == first["slug"]
    assert second["title"] == "PR 42"
    assert conn.execute("SELECT count(*) c FROM folders").fetchone()["c"] == 1


def test_create_folder_rejects_bad_slug(conn):
    with pytest.raises(store.Invalid):
        store.create_folder(conn, "../etc", "nope", 7)


def test_add_post_extends_expiry(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)
    assert store.get_folder(conn, "s")["expires_at"] == 1000 + 7 * DAY

    t[0] = 1000 + 6 * DAY
    store.add_post(conn, "s", "opus", "agent", "t", "md", "body", ttl_days=7)
    assert store.get_folder(conn, "s")["expires_at"] == 1000 + 13 * DAY


def test_expired_folder_is_invisible_before_the_reaper_runs(conn, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    store.create_folder(conn, "s", "S", 7)

    t[0] = 1000 + 8 * DAY
    assert store.get_folder(conn, "s") is None
    assert store.list_folders(conn) == []
    assert conn.execute("SELECT count(*) c FROM folders").fetchone()["c"] == 1


def test_add_post_stores_source_and_rendered_html(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(conn, "s", "opus", "agent", "T", "md", "# hi", ttl_days=7)

    row = conn.execute("SELECT * FROM posts WHERE id = ?", (pid,)).fetchone()
    assert row["source"] == "# hi"
    assert "<h1>hi</h1>" in row["html"]


def test_add_post_sanitizes(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(
        conn, "s", "opus", "agent", "T", "html", "<script>alert(1)</script>", ttl_days=7
    )
    row = conn.execute("SELECT html FROM posts WHERE id = ?", (pid,)).fetchone()
    assert "<script" not in row["html"]


def test_add_post_stores_images_and_resolves_references(conn):
    store.create_folder(conn, "s", "S", 7)
    pid = store.add_post(
        conn, "s", "opus", "agent", "T", "md", "![a](img:arch.png)",
        images=[("arch.png", PNG)], ttl_days=7,
    )

    blob = conn.execute("SELECT * FROM blobs WHERE post_id = ?", (pid,)).fetchone()
    assert blob["mime"] == "image/png"
    assert blob["folder"] == "s"

    html = conn.execute("SELECT html FROM posts WHERE id = ?", (pid,)).fetchone()["html"]
    assert f'src="/f/s/blob/{blob["id"]}"' in html


def test_add_post_rejects_a_png_that_is_actually_html(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(
            conn, "s", "opus", "agent", "T", "md", "x",
            images=[("evil.png", b"<html><script>alert(1)</script></html>")], ttl_days=7,
        )


def test_add_post_to_missing_folder_raises(conn):
    with pytest.raises(store.NotFound):
        store.add_post(conn, "nope", "opus", "agent", "T", "md", "x", ttl_days=7)


def test_add_post_rejects_unknown_format(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.add_post(conn, "s", "opus", "agent", "T", "pdf", "x", ttl_days=7)


def test_list_posts_since_returns_only_newer(conn):
    store.create_folder(conn, "s", "S", 7)
    first = store.add_post(conn, "s", "opus", "agent", "1", "md", "a", ttl_days=7)
    second = store.add_post(conn, "s", "opus", "agent", "2", "md", "b", ttl_days=7)

    assert [p["id"] for p in store.list_posts(conn, "s")] == [first, second]
    assert [p["id"] for p in store.list_posts(conn, "s", since=first)] == [second]


def test_set_status_rejects_unknown_status(conn):
    store.create_folder(conn, "s", "S", 7)
    with pytest.raises(store.Invalid):
        store.set_status(conn, "s", "sideways", None)


def test_set_status_updates_status_and_owner(conn):
    store.create_folder(conn, "s", "S", 7)
    store.set_status(conn, "s", "claimed", "opus-desktop")

    row = store.get_folder(conn, "s")
    assert row["status"] == "claimed"
    assert row["owner"] == "opus-desktop"


def test_blob_is_scoped_to_its_folder(conn):
    store.create_folder(conn, "a", "A", 7)
    store.create_folder(conn, "b", "B", 7)
    store.add_post(conn, "a", "opus", "agent", "T", "md", "x",
                   images=[("i.png", PNG)], ttl_days=7)
    blob_id = conn.execute("SELECT id FROM blobs").fetchone()["id"]

    assert store.get_blob(conn, "a", blob_id) is not None
    assert store.get_blob(conn, "b", blob_id) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff.store'`.

- [ ] **Step 3: Write `src/handoff/store.py`**

```python
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
    """Reduce a client-supplied name to a bare, substitution-safe filename."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")
    return cleaned[:64] or "file"


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
    prepared = []
    blob_urls = {}
    for raw_name, data in images:
        if len(data) > MAX_IMAGE_BYTES:
            raise Invalid(f"image too large: {raw_name}")
        mime = sniff_mime(data)
        if mime is None:
            raise Invalid(f"not an allowed image type: {raw_name}")
        name = safe_filename(raw_name)
        blob_id = secrets.token_hex(16)
        prepared.append((blob_id, name, mime, data))
        blob_urls[name] = f"/f/{slug}/blob/{blob_id}"
        blob_urls[raw_name] = f"/f/{slug}/blob/{blob_id}"

    html = render(body, fmt, blob_urls)
    now = clock.now()

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
    return post_id
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: PASS, 39 tests.

- [ ] **Step 5: Commit**

```bash
git add src/handoff/store.py tests/test_store.py
git commit -m "feat: folder, post, and blob store with sliding expiry"
```

---

### Task 5: App factory, CSP, and the agent JSON API

**Files:**
- Create: `src/handoff/app.py`, `src/handoff/api.py`
- Test: `tests/test_api.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces:
  - `app.create_app(db_path: str, ttl_days: int = 7) -> FastAPI`
  - `app.get_conn(request) -> sqlite3.Connection` — FastAPI dependency, one connection per request.
  - `app.CSP: str`
  - `api.router: APIRouter`, `api.require_agent(...)` dependency
  - pytest fixtures `client` (a `TestClient`) and `agent_token`.

The bearer dependency reads only the `Authorization` header. It never inspects cookies, which is what makes `/api/*` immune to CSRF: a browser cannot be tricked into attaching a credential the endpoint does not look for.

- [ ] **Step 1: Write the failing tests**

Extend `tests/conftest.py`. Its import block already has `import pytest` and
`from handoff import db` — **add only the missing names**, do not repeat the existing ones,
or ruff will fail on `F811` redefinition:

```python
# add to the existing imports at the top of conftest.py
from fastapi.testclient import TestClient

from handoff import app as app_module
from handoff import auth
```

Then append the fixtures:

```python
@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "handoff.db")


@pytest.fixture
def client(db_path):
    application = app_module.create_app(db_path, ttl_days=7)
    with TestClient(application) as c:
        yield c


@pytest.fixture
def agent_token(db_path):
    c = db.connect(db_path)
    db.init_schema(c)
    _, token = auth.mint_agent(c, "opus-desktop")
    c.close()
    return token


@pytest.fixture
def agent(agent_token):
    return {"Authorization": f"Bearer {agent_token}"}
```

`tests/test_api.py`:

```python
import base64

from handoff import clock

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_unauthenticated_request_is_rejected(client):
    assert client.get("/api/folders").status_code == 401


def test_bad_token_is_rejected(client, agent_token):
    r = client.get("/api/folders", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_create_and_list_folder(client, agent):
    r = client.post("/api/folders", json={"slug": "myslop-pr-42", "title": "PR 42"}, headers=agent)
    assert r.status_code == 200
    assert r.json()["slug"] == "myslop-pr-42"
    assert r.json()["status"] == "open"

    listed = client.get("/api/folders", headers=agent).json()
    assert [f["slug"] for f in listed] == ["myslop-pr-42"]


def test_create_folder_is_idempotent(client, agent):
    body = {"slug": "s", "title": "S"}
    assert client.post("/api/folders", json=body, headers=agent).status_code == 200
    assert client.post("/api/folders", json=body, headers=agent).status_code == 200
    assert len(client.get("/api/folders", headers=agent).json()) == 1


def test_create_folder_rejects_traversal_slug(client, agent):
    r = client.post("/api/folders", json={"slug": "../etc", "title": "x"}, headers=agent)
    assert r.status_code == 400


def test_post_and_read_back_source_not_html(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={"title": "handoff", "format": "md", "body": "# done",
              "author_note": "opus-5 on desktop"},
        headers=agent,
    )
    assert r.status_code == 200

    folder = client.get("/api/folders/s", headers=agent).json()
    post = folder["posts"][0]
    assert post["body"] == "# done"
    assert "<h1>" not in post["body"]
    assert post["author"] == "opus-desktop"
    assert post["author_note"] == "opus-5 on desktop"


def test_author_cannot_be_spoofed_from_the_body(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "x", "author": "someone-else",
              "author_kind": "human"},
        headers=agent,
    )
    post = client.get("/api/folders/s", headers=agent).json()["posts"][0]
    assert post["author"] == "opus-desktop"


def test_since_returns_only_newer_posts(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    client.post("/api/folders/s/posts", json={"format": "md", "body": "a"}, headers=agent)
    client.post("/api/folders/s/posts", json={"format": "md", "body": "b"}, headers=agent)

    first_id = client.get("/api/folders/s", headers=agent).json()["posts"][0]["id"]
    later = client.get(f"/api/folders/s?since={first_id}", headers=agent).json()["posts"]
    assert [p["body"] for p in later] == ["b"]


def test_post_can_set_status_and_owner_atomically(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "taking this", "status": "claimed",
              "owner": "opus-desktop"},
        headers=agent,
    )
    folder = client.get("/api/folders/s", headers=agent).json()
    assert folder["status"] == "claimed"
    assert folder["owner"] == "opus-desktop"


def test_status_endpoint_rejects_unknown_status(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post("/api/folders/s/status", json={"status": "sideways"}, headers=agent)
    assert r.status_code == 400


def test_image_upload_round_trip(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={
            "format": "md",
            "body": "![a](img:arch.png)",
            "images": [{"filename": "arch.png",
                        "content_b64": base64.b64encode(PNG).decode()}],
        },
        headers=agent,
    )
    assert r.status_code == 200
    assert r.json()["images"][0]["url"].startswith("/f/s/blob/")


def test_image_that_is_not_an_image_is_rejected(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "x",
              "images": [{"filename": "evil.png",
                          "content_b64": base64.b64encode(b"<script>").decode()}]},
        headers=agent,
    )
    assert r.status_code == 400


def test_malformed_base64_is_rejected(client, agent):
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    r = client.post(
        "/api/folders/s/posts",
        json={"format": "md", "body": "x",
              "images": [{"filename": "a.png", "content_b64": "!!!not base64!!!"}]},
        headers=agent,
    )
    assert r.status_code == 400


def test_missing_folder_returns_404(client, agent):
    assert client.get("/api/folders/nope", headers=agent).status_code == 404


def test_expired_folder_is_gone_from_the_api(client, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    client.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)

    t[0] += 8 * DAY
    assert client.get("/api/folders/s", headers=agent).status_code == 404
    assert client.get("/api/folders", headers=agent).json() == []


def test_security_headers_are_present(client, agent):
    r = client.get("/api/folders", headers=agent)
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_api_ignores_cookie_credentials(client, agent_token, db_path):
    from handoff import auth, db

    c = db.connect(db_path)
    uid = auth.create_user(c, "yoshi", "hunter2")
    sid = auth.create_session(c, uid)
    c.close()

    client.cookies.set(auth.COOKIE_NAME, sid)
    assert client.get("/api/folders").status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff.app'`.

- [ ] **Step 3: Write `src/handoff/app.py`**

```python
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
    def security_headers(request: Request, call_next):
        if request.headers.get("content-length", "").isdigit():
            if int(request.headers["content-length"]) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "body too large"}, status_code=413)
        response = call_next(request)
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
```

Note: FastAPI's `@app.middleware("http")` expects an async callable. Define it as `async def security_headers(request, call_next)` and `await call_next(request)`. Middleware is not a route handler, so the sync-handler constraint does not apply to it. Correct the snippet above accordingly when writing the file.

- [ ] **Step 4: Write `src/handoff/api.py`**

```python
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
    images = []
    for img in body.images:
        try:
            data = base64.b64decode(img.content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise store.Invalid(f"bad base64 for {img.filename}") from exc
        images.append((img.filename, data))

    post_id = store.add_post(
        conn, slug, agent["name"], "agent", body.title, body.format, body.body,
        author_note=body.author_note, images=images,
        ttl_days=request.app.state.ttl_days,
    )
    if body.status is not None:
        store.set_status(conn, slug, body.status, body.owner)

    blobs = conn.execute(
        "SELECT id, filename FROM blobs WHERE post_id = ?", (post_id,)
    ).fetchall()
    return {
        "id": post_id,
        "images": [{"filename": b["filename"], "url": f"/f/{slug}/blob/{b['id']}"}
                   for b in blobs],
    }


@router.post("/folders/{slug}/status")
def set_status(
    slug: str,
    body: StatusUpdate,
    agent: sqlite3.Row = Depends(require_agent),
    conn: sqlite3.Connection = Depends(get_conn),
):
    store.set_status(conn, slug, body.status, body.owner)
    return _folder_json(store.get_folder(conn, slug))
```

- [ ] **Step 5: Create the empty static directory and a stub `web.py` so the app imports**

`src/handoff/static/style.css` — one line for now, replaced in Task 7:

```css
body { font-family: system-ui, sans-serif; }
```

`src/handoff/web.py` — replaced in Task 6:

```python
"""Human-facing HTML routes."""

from fastapi import APIRouter

router = APIRouter()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: PASS, 17 tests.

If `test_expired_folder_is_gone_from_the_api` fails because `db.reap` deleted the row before `store.get_folder` filtered it, that is still a pass condition — both paths must yield 404. If it fails with 200, the read filter is missing.

- [ ] **Step 7: Run the whole suite and ruff**

Run: `.venv/bin/pytest -v && .venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/handoff/app.py src/handoff/api.py src/handoff/web.py src/handoff/static tests
git commit -m "feat: app factory with CSP and the agent JSON API"
```

---

### Task 6: Human login and session handling

**Files:**
- Modify: `src/handoff/web.py`
- Create: `src/handoff/templates/base.html`, `src/handoff/templates/login.html`
- Test: `tests/test_web_auth.py`

**Interfaces:**
- Consumes: `auth.*`, `app.get_conn`.
- Produces:
  - `web.router` with `GET /login`, `POST /login`, `POST /logout`
  - `web.require_user(request, conn) -> sqlite3.Row` — dependency; raises a 303 redirect to `/login` when unauthenticated.
  - `web.templates: Jinja2Templates`
  - `web.require_csrf(request, sid) -> None`
  - pytest fixture `human` — a `TestClient` already logged in.

- [ ] **Step 1: Write the failing tests**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def human(client, db_path):
    c = db.connect(db_path)
    auth.create_user(c, "yoshi", "hunter2")
    c.close()
    auth.reset_throttle()
    client.post("/login", data={"username": "yoshi", "password": "hunter2"},
                follow_redirects=False)
    return client
```

`tests/test_web_auth.py`:

```python
from handoff import auth, db


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "password" in r.text


def test_index_redirects_when_logged_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def _make_user(db_path):
    c = db.connect(db_path)
    auth.create_user(c, "yoshi", "hunter2")
    c.close()
    auth.reset_throttle()


def test_login_sets_a_hardened_cookie(client, db_path):
    _make_user(db_path)

    r = client.post("/login", data={"username": "yoshi", "password": "hunter2"},
                    follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_cookie_is_secure_under_tls(db_path):
    from fastapi.testclient import TestClient

    from handoff import app as app_module

    _make_user(db_path)
    with TestClient(app_module.create_app(db_path), base_url="https://testserver") as tls:
        r = tls.post("/login", data={"username": "yoshi", "password": "hunter2"},
                     follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


def test_cookie_is_not_secure_over_plain_http(client, db_path):
    # Verified against httpx/TestClient: a Secure cookie set over http is never sent back,
    # exactly as a browser behaves. Marking it Secure on the documented plain-http tailnet
    # deployment would not harden the session -- it would make login impossible.
    _make_user(db_path)

    r = client.post("/login", data={"username": "yoshi", "password": "hunter2"},
                    follow_redirects=False)
    assert "secure" not in r.headers["set-cookie"].lower()


def test_bad_password_does_not_log_in(client, db_path):
    c = db.connect(db_path)
    auth.create_user(c, "yoshi", "hunter2")
    c.close()
    auth.reset_throttle()

    r = client.post("/login", data={"username": "yoshi", "password": "nope"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert auth.COOKIE_NAME not in r.cookies


def test_logged_in_user_reaches_the_index(human):
    r = human.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_logout_clears_the_session(human):
    token = human.get("/").text
    csrf = token.split('name="csrf" value="')[1].split('"')[0]

    human.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert human.get("/", follow_redirects=False).status_code == 303


def test_logout_without_csrf_is_rejected(human):
    r = human.post("/logout", data={}, follow_redirects=False)
    assert r.status_code == 403
    assert human.get("/", follow_redirects=False).status_code == 200


def test_no_page_contains_a_script_tag(human):
    for path in ["/login", "/"]:
        assert "<script" not in human.get(path).text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_auth.py -v`
Expected: FAIL — 404 on `/login`.

- [ ] **Step 3: Write the templates**

`src/handoff/templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}handoff{% endblock %}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <a href="/">handoff</a>
    {% if user %}
      <nav>
        <a href="/agents">agents</a>
        <form method="post" action="/logout" class="inline">
          <input type="hidden" name="csrf" value="{{ csrf }}">
          <button type="submit">log out {{ user.username }}</button>
        </form>
      </nav>
    {% endif %}
  </header>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`src/handoff/templates/login.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Log in</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/login">
  <label>Username <input name="username" autocomplete="username" required></label>
  <label>Password <input name="password" type="password"
         autocomplete="current-password" required></label>
  <button type="submit">Log in</button>
</form>
{% endblock %}
```

- [ ] **Step 4: Write `src/handoff/web.py`**

```python
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


def require_user(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> sqlite3.Row:
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
    sid = request.cookies.get(auth.COOKIE_NAME, "")
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
        auth.COOKIE_NAME, sid, httponly=True, samesite="lax",
        # Secure whenever TLS is actually in play -- including behind a terminating proxy,
        # because uvicorn runs with proxy_headers=True and honours X-Forwarded-Proto.
        # Over plain http a Secure cookie is simply never sent back, so hardcoding it
        # would break login on the documented tailnet deployment while protecting nothing
        # (tailnet traffic is already WireGuard-encrypted).
        secure=request.url.scheme == "https",
        max_age=auth.SESSION_TTL, path="/",
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
```

- [ ] **Step 5: Add the redirect exception handler to `create_app`**

In `src/handoff/app.py`, inside `create_app`, after the existing handlers:

```python
    from handoff.web import LoginRequired

    @application.exception_handler(LoginRequired)
    def _login_required(request: Request, exc: LoginRequired):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/login", status_code=303)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_web_auth.py -v`
Expected: FAIL only on the four tests that need `GET /`, which does not exist yet — `test_index_redirects_when_logged_out`, `test_logged_in_user_reaches_the_index`, `test_logout_clears_the_session`, and `test_no_page_contains_a_script_tag`. The other six must pass: `test_login_page_renders`, `test_login_sets_a_hardened_cookie`, `test_cookie_is_secure_under_tls`, `test_cookie_is_not_secure_over_plain_http`, `test_bad_password_does_not_log_in`, `test_logout_without_csrf_is_rejected`.

- [ ] **Step 7: Add a minimal `GET /` so the remaining tests pass**

Append to `src/handoff/web.py`. It is replaced with the real index in Task 7:

```python
@router.get("/")
def index(request: Request, user: sqlite3.Row = Depends(require_user)):
    return page(request, "index.html", user=user, folders=[])
```

And `src/handoff/templates/index.html`:

```html
{% extends "base.html" %}
{% block content %}<h1>Hand-offs</h1>{% endblock %}
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_auth.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 9: Commit**

```bash
git add src/handoff/web.py src/handoff/app.py src/handoff/templates tests/test_web_auth.py tests/conftest.py
git commit -m "feat: human login, hardened session cookie, and CSRF"
```

---

### Task 7: Human index, thread page, and blob serving

**Files:**
- Modify: `src/handoff/web.py`, `src/handoff/templates/index.html`
- Create: `src/handoff/templates/folder.html`, `src/handoff/static/style.css` (replace stub)
- Test: `tests/test_web_pages.py`

**Interfaces:**
- Consumes: `store.*`, `web.require_user`, `web.page`.
- Produces: `GET /`, `GET /f/{slug}`, `GET /f/{slug}/blob/{id}`, and the Jinja filter `expires_in`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web_pages.py`:

```python
import base64

from handoff import clock

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _seed(client, agent, slug="myslop-pr-42", body="# hello", images=None):
    client.post("/api/folders", json={"slug": slug, "title": "PR 42"}, headers=agent)
    payload = {"format": "md", "body": body, "title": "handoff",
               "author_note": "opus-5 on desktop"}
    if images:
        payload["images"] = images
    return client.post(f"/api/folders/{slug}/posts", json=payload, headers=agent).json()


def test_index_lists_live_folders_with_status_and_expiry(human, agent):
    _seed(human, agent)
    body = human.get("/").text
    assert "myslop-pr-42" in body
    assert "open" in body
    assert "expires in" in body


def test_index_hides_expired_folders(human, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    _seed(human, agent)

    t[0] += 8 * DAY
    assert "myslop-pr-42" not in human.get("/").text


def test_thread_renders_markdown_as_html(human, agent):
    _seed(human, agent, body="# hello")
    body = human.get("/f/myslop-pr-42").text
    assert "<h1>hello</h1>" in body


def test_thread_shows_author_and_self_reported_note(human, agent):
    _seed(human, agent)
    body = human.get("/f/myslop-pr-42").text
    assert "opus-desktop" in body
    assert "opus-5 on desktop" in body


def test_thread_does_not_execute_injected_script(human, agent):
    human.post("/api/folders", json={"slug": "s", "title": "S"}, headers=agent)
    human.post("/api/folders/s/posts",
               json={"format": "html", "body": "<script>alert(1)</script><p>ok</p>"},
               headers=agent)

    body = human.get("/f/s").text
    assert "<script" not in body.lower()
    assert "<p>ok</p>" in body


def test_missing_folder_returns_404(human):
    assert human.get("/f/nope").status_code == 404


def test_blob_is_served_with_sniffed_type_and_hardening(human, agent):
    result = _seed(human, agent, slug="s", body="![a](img:a.png)",
                   images=[{"filename": "a.png",
                            "content_b64": base64.b64encode(PNG).decode()}])
    url = result["images"][0]["url"]

    r = human.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_blob_from_another_folder_does_not_resolve(human, agent):
    result = _seed(human, agent, slug="a", body="x",
                   images=[{"filename": "a.png",
                            "content_b64": base64.b64encode(PNG).decode()}])
    blob_id = result["images"][0]["url"].rsplit("/", 1)[-1]
    human.post("/api/folders", json={"slug": "b", "title": "B"}, headers=agent)

    assert human.get(f"/f/b/blob/{blob_id}").status_code == 404


def test_pages_require_login(client, agent):
    _seed(client, agent)
    assert client.get("/f/myslop-pr-42", follow_redirects=False).status_code == 303
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_pages.py -v`
Expected: FAIL — 404 on `/f/{slug}`, and the index assertions fail against the stub.

- [ ] **Step 3: Replace `src/handoff/templates/index.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>Hand-offs</h1>
{% if not folders %}<p class="empty">Nothing in flight.</p>{% endif %}
<ul class="folders">
  {% for f in folders %}
  <li>
    <a href="/f/{{ f.slug }}">{{ f.slug }}</a>
    <span class="status status-{{ f.status }}">{{ f.status }}</span>
    {% if f.owner %}<span class="owner">{{ f.owner }}</span>{% endif %}
    <span class="expiry">expires in {{ f.expires_at | expires_in }}</span>
  </li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 4: Create `src/handoff/templates/folder.html`**

`post.html` is already sanitized by `render.render`, so `| safe` here is deliberate and is the only place it appears.

```html
{% extends "base.html" %}
{% block title %}{{ folder.slug }}{% endblock %}
{% block content %}
<h1>{{ folder.title }}</h1>
<p class="meta">
  <code>{{ folder.slug }}</code>
  <span class="status status-{{ folder.status }}">{{ folder.status }}</span>
  {% if folder.owner %}<span class="owner">owner: {{ folder.owner }}</span>{% endif %}
  <span class="expiry">expires in {{ folder.expires_at | expires_in }}</span>
</p>

{% for p in posts %}
<article class="post">
  <h2>{{ p.title or "(untitled)" }}</h2>
  <p class="byline">
    <strong>{{ p.author }}</strong>
    <span class="kind">{{ p.author_kind }}</span>
    {% if p.author_note %}<span class="note">says: {{ p.author_note }}</span>{% endif %}
  </p>
  <div class="body">{{ p.html | safe }}</div>
</article>
{% endfor %}
{% endblock %}
```

- [ ] **Step 5: Add the routes to `src/handoff/web.py`**

Replace the stub `index` from Task 6 with this, and add the rest:

```python
from fastapi import Response

from handoff import clock, db, store


def _expires_in(expires_at: int) -> str:
    seconds = max(0, expires_at - clock.now())
    days, rem = divmod(seconds, 86400)
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = rem // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


templates.env.filters["expires_in"] = _expires_in


@router.get("/")
def index(
    request: Request,
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    db.reap(conn)
    return page(request, "index.html", user=user, folders=store.list_folders(conn))


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
        request, "folder.html", user=user,
        folder=folder, posts=store.list_posts(conn, slug), statuses=store.STATUSES,
    )


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
```

This route depends on the `setdefault` behaviour already written into the `security_headers` middleware in Task 5. If `test_blob_is_served_with_sniffed_type_and_hardening` fails on the `sandbox` assertion, the middleware is overwriting the route's CSP — check that it uses `setdefault` and not plain assignment.

- [ ] **Step 6: Write `src/handoff/static/style.css`**

```css
:root { color-scheme: light dark; }
body {
  font-family: system-ui, sans-serif;
  line-height: 1.5;
  max-width: 52rem;
  margin: 0 auto;
  padding: 1rem;
}
header { display: flex; justify-content: space-between; align-items: baseline; }
header nav { display: flex; gap: 1rem; align-items: baseline; }
form.inline { display: inline; }
.status { border-radius: 0.25rem; padding: 0 0.4rem; font-size: 0.85rem; }
.status-open { background: #2d6; color: #000; }
.status-claimed { background: #59f; color: #000; }
.status-blocked { background: #f73; color: #000; }
.status-done { background: #999; color: #000; }
.expiry, .owner, .kind, .note { color: #888; font-size: 0.85rem; }
.folders { list-style: none; padding: 0; }
.folders li { display: flex; gap: 0.75rem; align-items: baseline; padding: 0.35rem 0; }
.post { border-top: 1px solid #8884; padding-top: 0.5rem; margin-top: 1.5rem; }
.post .body img { max-width: 100%; }
.post .body pre { overflow-x: auto; background: #8881; padding: 0.5rem; }
table { border-collapse: collapse; }
th, td { border: 1px solid #8886; padding: 0.25rem 0.5rem; }
.error { color: #c33; }
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_pages.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 8: Commit**

```bash
git add src/handoff/web.py src/handoff/templates src/handoff/static tests/test_web_pages.py
git commit -m "feat: human index, thread rendering, and hardened blob serving"
```

---

### Task 8: Human posting and status changes

**Files:**
- Modify: `src/handoff/web.py`, `src/handoff/templates/folder.html`
- Test: `tests/test_web_write.py`

**Interfaces:**
- Consumes: `store.add_post`, `store.set_status`, `web.require_csrf`.
- Produces: `POST /f/{slug}/post`, `POST /f/{slug}/status`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web_write.py`:

```python
from handoff import clock

DAY = 86400


def _csrf(client, path="/"):
    return client.get(path).text.split('name="csrf" value="')[1].split('"')[0]


def _seed(client, agent, slug="s"):
    client.post("/api/folders", json={"slug": slug, "title": "S"}, headers=agent)


def test_human_post_appears_in_the_thread(human, agent):
    _seed(human, agent)
    csrf = _csrf(human, "/f/s")

    r = human.post("/f/s/post",
                   data={"csrf": csrf, "title": "answer", "body": "**do it**"},
                   follow_redirects=False)
    assert r.status_code == 303

    body = human.get("/f/s").text
    assert "<strong>do it</strong>" in body
    assert "yoshi" in body


def test_human_post_is_attributed_to_the_logged_in_user(human, agent):
    _seed(human, agent)
    human.post("/f/s/post", data={"csrf": _csrf(human, "/f/s"), "body": "x",
                                  "author": "somebody-else"},
               follow_redirects=False)

    post = human.get("/api/folders/s", headers=agent).json()["posts"][0]
    assert post["author"] == "yoshi"
    assert post["author_kind"] == "human"


def test_human_post_is_sanitized(human, agent):
    _seed(human, agent)
    human.post("/f/s/post",
               data={"csrf": _csrf(human, "/f/s"), "body": "<script>alert(1)</script>",
                     "format": "html"},
               follow_redirects=False)
    assert "<script" not in human.get("/f/s").text.lower()


def test_human_post_extends_expiry(human, agent, monkeypatch):
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    _seed(human, agent)

    t[0] += 6 * DAY
    human.post("/f/s/post", data={"csrf": _csrf(human, "/f/s"), "body": "still alive"},
               follow_redirects=False)

    folder = human.get("/api/folders/s", headers=agent).json()
    assert folder["expires_at"] == t[0] + 7 * DAY


def test_human_can_set_status(human, agent):
    _seed(human, agent)
    human.post("/f/s/status",
               data={"csrf": _csrf(human, "/f/s"), "status": "done", "owner": "yoshi"},
               follow_redirects=False)

    folder = human.get("/api/folders/s", headers=agent).json()
    assert folder["status"] == "done"
    assert folder["owner"] == "yoshi"


def test_post_without_csrf_is_rejected(human, agent):
    _seed(human, agent)
    r = human.post("/f/s/post", data={"body": "x"}, follow_redirects=False)
    assert r.status_code == 403
    assert human.get("/api/folders/s", headers=agent).json()["posts"] == []


def test_status_without_csrf_is_rejected(human, agent):
    _seed(human, agent)
    r = human.post("/f/s/status", data={"status": "done"}, follow_redirects=False)
    assert r.status_code == 403


def test_bad_status_is_rejected(human, agent):
    _seed(human, agent)
    r = human.post("/f/s/status", data={"csrf": _csrf(human, "/f/s"), "status": "sideways"},
                   follow_redirects=False)
    assert r.status_code == 400


def test_logged_out_user_cannot_post(client, agent):
    _seed(client, agent)
    r = client.post("/f/s/post", data={"csrf": "x", "body": "x"}, follow_redirects=False)
    assert r.status_code == 303


def test_there_is_no_folder_delete_route(human, agent):
    _seed(human, agent)
    assert human.delete("/f/s").status_code in (404, 405)
    assert human.post("/f/s/delete", data={"csrf": _csrf(human, "/f/s")}).status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_write.py -v`
Expected: FAIL — 404 on `/f/s/post`.

- [ ] **Step 3: Add the forms to `src/handoff/templates/folder.html`**

Insert before `{% endblock %}`:

```html
<section class="compose">
  <h2>Reply</h2>
  <form method="post" action="/f/{{ folder.slug }}/post">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Title <input name="title"></label>
    <label>Body
      <textarea name="body" rows="8" required
                placeholder="Markdown. What you decided, what's next."></textarea>
    </label>
    <input type="hidden" name="format" value="md">
    <button type="submit">Post</button>
  </form>

  <form method="post" action="/f/{{ folder.slug }}/status" class="status-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Status
      <select name="status">
        {% for s in statuses %}
        <option value="{{ s }}" {% if s == folder.status %}selected{% endif %}>{{ s }}</option>
        {% endfor %}
      </select>
    </label>
    <label>Owner <input name="owner" value="{{ folder.owner or '' }}"></label>
    <button type="submit">Set</button>
  </form>
</section>
```

- [ ] **Step 4: Add the routes to `src/handoff/web.py`**

```python
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
        conn, slug, user["username"], "human", title or None, format, body,
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
```

Dependency order matters: `require_user` must resolve before `require_csrf` runs, so a logged-out POST redirects rather than returning 403. FastAPI resolves declared dependencies before the body executes, which gives that order for free.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_write.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 6: Commit**

```bash
git add src/handoff/web.py src/handoff/templates/folder.html tests/test_web_write.py
git commit -m "feat: human replies and status changes"
```

---

### Task 9: Agent administration UI

**Files:**
- Modify: `src/handoff/web.py`
- Create: `src/handoff/templates/agents.html`
- Test: `tests/test_web_agents.py`

**Interfaces:**
- Consumes: `auth.mint_agent`, `auth.list_agents`, `auth.revoke_agent`.
- Produces: `GET /agents`, `POST /agents`, `POST /agents/{agent_id}/revoke`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web_agents.py`:

```python
def _csrf(client, path="/agents"):
    return client.get(path).text.split('name="csrf" value="')[1].split('"')[0]


def test_minting_shows_the_token_once(human):
    r = human.post("/agents", data={"csrf": _csrf(human), "name": "opus-laptop"},
                   follow_redirects=True)
    assert "opus-laptop" in r.text
    token = r.text.split('class="token">')[1].split("<")[0].strip()
    assert len(token) == 36

    assert token not in human.get("/agents").text


def test_minted_token_authenticates(human):
    r = human.post("/agents", data={"csrf": _csrf(human), "name": "opus-laptop"},
                   follow_redirects=True)
    token = r.text.split('class="token">')[1].split("<")[0].strip()

    headers = {"Authorization": f"Bearer {token}"}
    assert human.get("/api/folders", headers=headers).status_code == 200


def test_revoked_token_stops_working(human):
    r = human.post("/agents", data={"csrf": _csrf(human), "name": "opus-laptop"},
                   follow_redirects=True)
    token = r.text.split('class="token">')[1].split("<")[0].strip()
    agent_id = r.text.split('action="/agents/')[1].split("/revoke")[0]

    human.post(f"/agents/{agent_id}/revoke", data={"csrf": _csrf(human)},
               follow_redirects=False)

    headers = {"Authorization": f"Bearer {token}"}
    assert human.get("/api/folders", headers=headers).status_code == 401


def test_duplicate_agent_name_is_reported_not_crashed(human):
    human.post("/agents", data={"csrf": _csrf(human), "name": "dup"}, follow_redirects=True)
    r = human.post("/agents", data={"csrf": _csrf(human), "name": "dup"},
                   follow_redirects=True)
    assert r.status_code == 200
    assert "already in use" in r.text


def test_mint_requires_csrf(human):
    r = human.post("/agents", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 403


def test_agents_page_requires_login(client):
    assert client.get("/agents", follow_redirects=False).status_code == 303
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_agents.py -v`
Expected: FAIL — 404 on `/agents`.

- [ ] **Step 3: Create `src/handoff/templates/agents.html`**

```html
{% extends "base.html" %}
{% block title %}agents{% endblock %}
{% block content %}
<h1>Agents</h1>

{% if error %}<p class="error">{{ error }}</p>{% endif %}

{% if new_token %}
<div class="minted">
  <p><strong>{{ new_name }}</strong> created. Copy this token now &mdash;
     it is not stored and cannot be shown again.</p>
  <p><code class="token">{{ new_token }}</code></p>
</div>
{% endif %}

<form method="post" action="/agents">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <label>Name <input name="name" required placeholder="opus-desktop"></label>
  <button type="submit">Mint token</button>
</form>

<ul class="agents">
  {% for a in agents %}
  <li>
    <strong>{{ a.name }}</strong>
    {% if a.revoked_at %}
      <span class="status status-done">revoked</span>
    {% else %}
      <form method="post" action="/agents/{{ a.id }}/revoke" class="inline">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button type="submit">Revoke</button>
      </form>
    {% endif %}
  </li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 4: Add the routes to `src/handoff/web.py`**

The freshly minted token is passed straight to the template and never persisted, so a page refresh cannot re-reveal it.

```python
@router.get("/agents")
def agents_page(
    request: Request,
    user: sqlite3.Row = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return page(request, "agents.html", user=user, agents=auth.list_agents(conn),
                new_token=None, new_name=None, error=None)


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
        return page(request, "agents.html", user=user, agents=auth.list_agents(conn),
                    new_token=None, new_name=None, error=str(exc))
    return page(request, "agents.html", user=user, agents=auth.list_agents(conn),
                new_token=token, new_name=name, error=None)


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
```

`auth.mint_agent` raises `ValueError("agent name already in use: ...")`, which is what `test_duplicate_agent_name_is_reported_not_crashed` matches on.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_agents.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest -v && .venv/bin/ruff check . && .venv/bin/ruff format .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/handoff/web.py src/handoff/templates/agents.html tests/test_web_agents.py
git commit -m "feat: agent token administration UI"
```

---

### Task 10: CLI and bootstrap safety

**Files:**
- Create: `src/handoff/cli.py`, `src/handoff/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `auth.create_user`, `db.*`, `app.create_app`.
- Produces:
  - `cli.main(argv: list[str] | None = None) -> int`
  - Subcommands: `createuser <username>`, `reap`, `serve`.
  - `cli.user_count(conn) -> int`

`serve` refuses to start with zero users rather than creating a default one. There is no default credential anywhere in this project.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
from handoff import auth, cli, db

# Must be at least MIN_PASSWORD_LEN (12) or createuser rejects it before doing anything.
GOOD = "correct horse battery staple"
ALSO_GOOD = "a different long passphrase"


def test_createuser_creates_a_user(db_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": GOOD)
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 0

    c = db.connect(db_path)
    auth.reset_throttle()
    assert auth.verify_user(c, "yoshi", GOOD) is not None
    c.close()


def test_createuser_rejects_a_mismatched_confirmation(db_path, monkeypatch):
    # Both answers clear the length check, so this exercises the mismatch branch itself
    # rather than short-circuiting on length.
    answers = iter([GOOD, ALSO_GOOD])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 1

    c = db.connect(db_path)
    db.init_schema(c)
    assert cli.user_count(c) == 0
    c.close()


def test_createuser_rejects_a_duplicate_username(db_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": GOOD)
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 0
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 1


def test_createuser_rejects_a_short_password(db_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "short")
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 1


def test_reap_reports_what_it_deleted(db_path, capsys):
    assert cli.main(["--db", db_path, "reap"]) == 0
    assert "0" in capsys.readouterr().out


def test_serve_refuses_with_no_users(db_path, capsys):
    assert cli.main(["--db", db_path, "serve"]) == 1
    assert "createuser" in capsys.readouterr().err


def test_serve_never_defaults_to_all_interfaces():
    parser = cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.bind == "127.0.0.1"


def test_bind_to_all_interfaces_is_refused(db_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": GOOD)
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 0

    assert cli.main(["--db", db_path, "serve", "--bind", "0.0.0.0"]) == 1
    assert "0.0.0.0" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff.cli'`.

- [ ] **Step 3: Write `src/handoff/cli.py`**

```python
"""Command line entry points: createuser, reap, serve."""

import argparse
import getpass
import os
import sqlite3
import sys

from handoff import auth, db

MIN_PASSWORD_LEN = 12


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) c FROM users").fetchone()["c"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff")
    parser.add_argument("--db", default=os.environ.get("HANDOFF_DB", "handoff.db"))
    parser.add_argument(
        "--ttl-days", type=int, default=int(os.environ.get("HANDOFF_TTL_DAYS", "7"))
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("createuser", help="create a human login")
    create.add_argument("username")

    sub.add_parser("reap", help="delete expired folders now")

    serve = sub.add_parser("serve", help="run the service")
    serve.add_argument("--bind", default=os.environ.get("HANDOFF_BIND", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("HANDOFF_PORT", "8080")))
    return parser


def _createuser(conn: sqlite3.Connection, username: str) -> int:
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Password must be at least {MIN_PASSWORD_LEN} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Confirm: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        auth.create_user(conn, username, password)
    except sqlite3.IntegrityError:
        print(f"User already exists: {username}", file=sys.stderr)
        return 1
    print(f"Created user {username}.")
    return 0


def _serve(db_path: str, ttl_days: int, bind: str, port: int, conn: sqlite3.Connection) -> int:
    if bind == "0.0.0.0":  # noqa: S104
        print(
            "Refusing to bind 0.0.0.0. Bind a specific interface (your tailnet address)"
            " or 127.0.0.1 behind a reverse proxy.",
            file=sys.stderr,
        )
        return 1
    if user_count(conn) == 0:
        print(
            "No users exist. Run 'handoff createuser <name>' before serving.",
            file=sys.stderr,
        )
        return 1

    import uvicorn

    from handoff.app import create_app

    # proxy_headers honours X-Forwarded-Proto, so a TLS-terminating reverse proxy makes
    # request.url.scheme == "https" and the session cookie comes back marked Secure.
    uvicorn.run(
        create_app(db_path, ttl_days), host=bind, port=port,
        proxy_headers=True, forwarded_allow_ips="127.0.0.1",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    try:
        if args.command == "createuser":
            return _createuser(conn, args.username)
        if args.command == "reap":
            print(f"Deleted {db.reap(conn)} expired folders.")
            return 0
        if args.command == "serve":
            return _serve(args.db, args.ttl_days, args.bind, args.port, conn)
    finally:
        conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

`src/handoff/__main__.py`:

```python
from handoff.cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS, 8 tests.

Every test password except the one in `test_createuser_rejects_a_short_password` clears `MIN_PASSWORD_LEN`. Do not lower `MIN_PASSWORD_LEN` to make anything pass.

- [ ] **Step 5: Commit**

```bash
git add src/handoff/cli.py src/handoff/__main__.py tests/test_cli.py
git commit -m "feat: CLI with createuser, reap, and a serve that refuses unsafe bind"
```

---

### Task 11: The Skill

**Files:**
- Create: `skills/handoff/SKILL.md`
- Test: `tests/test_skill_doc.py`

**Interfaces:**
- Consumes: the API surface from Task 5.
- Produces: an installable skill directory.

The test exists because a skill that documents an endpoint the service does not serve is worse than no skill — the agent silently fails at exactly the moment it is trying to hand off work.

- [ ] **Step 1: Write the failing test**

`tests/test_skill_doc.py`:

```python
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "handoff" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_frontmatter_with_name_and_description():
    text = SKILL.read_text()
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    assert re.search(r"^name:\s*handoff\s*$", front, re.M)
    assert re.search(r"^description:\s*\S", front, re.M)


def test_every_documented_endpoint_exists_in_the_app(db_path):
    from handoff.app import create_app

    app = create_app(db_path)
    served = {(m, r.path) for r in app.routes for m in getattr(r, "methods", set())}

    documented = set(
        re.findall(r"\b(GET|POST)\s+(/api/[a-z0-9{}/_-]+)", SKILL.read_text())
    )
    assert documented, "skill documents no endpoints"

    normalised = {(m, re.sub(r"\{[a-z_]+\}", "{slug}", p.split("?")[0].rstrip("/")))
                  for m, p in documented}
    served_norm = {(m, re.sub(r"\{[a-z_]+\}", "{slug}", p.rstrip("/"))) for m, p in served}

    assert normalised <= served_norm, f"undocumented-or-wrong: {normalised - served_norm}"


def test_skill_states_the_expiry_rule():
    text = SKILL.read_text().lower()
    assert "7 day" in text or "seven day" in text
    assert "not" in text and "memory" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_skill_doc.py -v`
Expected: FAIL — the file does not exist.

- [ ] **Step 3: Write `skills/handoff/SKILL.md`**

````markdown
---
name: handoff
description: Use when handing work to another agent or another PC, when picking up work someone else started, when you need a decision from the human, or when you finish a stint and someone else will continue. Posts and reads hand-off notes on the shared handoff server.
---

# Hand-off

A shared board for passing work between agents on different machines, and to the human.

**Everything here is deleted seven days after the last post. This is not memory.**
Anything that must survive belongs in the repository.

## Setup

Two environment variables, set on this machine:

- `HANDOFF_URL` — e.g. `http://handoff.tailnet:8080`
- `HANDOFF_TOKEN` — the UUID the human minted for this machine

If `HANDOFF_TOKEN` is unset, tell the human: they mint one at `$HANDOFF_URL/agents`.
Do not invent a token and do not proceed without one.

## Folders

One folder per hand-off. Slugs are lowercase, digits and hyphens only, 64 characters max:

- `myslop-pr-42` — work on a pull request
- `kypost-tls-migration` — a named piece of project work
- `llamamail-build-break` — an incident

Name it after the thing being handed off, prefixed with the project. Reuse an existing
folder for a continuing hand-off; do not create `-v2`.

## Protocol

**Before you start work:**

```bash
curl -s -H "Authorization: Bearer $HANDOFF_TOKEN" "$HANDOFF_URL/api/folders/myslop-pr-42"
```

Read `status` and `owner`. If `status` is `claimed` and `owner` is not you, someone else
has it — do not duplicate the work. Read the posts before touching anything.

**When you pick it up**, claim it in the same call as your first post (below) rather than
as a separate step, so claiming and announcing cannot half-succeed.

**When you hand off**, post three things: what you did, what remains, and what is
dangerous. The next agent has none of your context.

**When you are blocked on the human**, set `status=blocked` and state exactly what
decision you need. Vague blocks stall for days.

**Statuses:** `open` (available), `claimed` (someone is on it), `blocked` (needs a human),
`done` (finished; left for the human to read).

## API

Every request carries `Authorization: Bearer $HANDOFF_TOKEN`.

### Create a folder — `POST /api/folders`

Idempotent; calling it on an existing folder returns that folder unchanged.

```bash
curl -s -X POST "$HANDOFF_URL/api/folders" \
  -H "Authorization: Bearer $HANDOFF_TOKEN" -H "Content-Type: application/json" \
  -d '{"slug": "myslop-pr-42", "title": "PR 42: token rotation"}'
```

### List live folders — `GET /api/folders`

```bash
curl -s -H "Authorization: Bearer $HANDOFF_TOKEN" "$HANDOFF_URL/api/folders"
```

### Read a folder — `GET /api/folders/{slug}`

Bodies come back as the original Markdown, not HTML. Add `?since=<post_id>` to fetch only
posts newer than one you have already read — use this when polling.

```bash
curl -s -H "Authorization: Bearer $HANDOFF_TOKEN" \
  "$HANDOFF_URL/api/folders/myslop-pr-42?since=7"
```

### Post — `POST /api/folders/{slug}/posts`

```bash
curl -s -X POST "$HANDOFF_URL/api/folders/myslop-pr-42/posts" \
  -H "Authorization: Bearer $HANDOFF_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "title": "Token rotation done, reload hook left",
    "format": "md",
    "author_note": "opus-5 on desktop, branch feat/rotation",
    "body": "## Done\n- Rotation lands in `auth.py`\n\n## Left\n- systemd reload hook\n\n## Careful\n- Old tokens stay valid until reload. Do not deploy without the hook.",
    "status": "open",
    "owner": null
  }'
```

Fields:

- `format` — `md` (default), `html`, or `text`.
- `author_note` — your self-description: model, machine, branch. The server records who you
  are from your token; this is the extra context only you know.
- `status` / `owner` — optional; set them here to claim or release in the same request.
- `images` — optional list of `{"filename": "...", "content_b64": "..."}`. PNG, JPEG, GIF
  and WebP only, 5 MB each, 10 MB per request. Reference one in Markdown as
  `![alt](img:filename.png)` and it resolves to the stored image.

Your author name comes from the token and cannot be set from the body. Do not try.

### Set status without posting — `POST /api/folders/{slug}/status`

```bash
curl -s -X POST "$HANDOFF_URL/api/folders/myslop-pr-42/status" \
  -H "Authorization: Bearer $HANDOFF_TOKEN" -H "Content-Type: application/json" \
  -d '{"status": "claimed", "owner": "opus-desktop"}'
```

## Rules

1. Never post secrets, tokens, or credentials. The human reads this in a browser and it
   sits on disk for a week.
2. Do not use this as a scratchpad or a memory store. It expires.
3. One folder per hand-off. Do not create a folder per post.
4. Write for someone with none of your context: what you did, what is left, what will bite.
5. There is no delete. A post is permanent until the folder expires. Think first.
````

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_skill_doc.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add skills/handoff/SKILL.md tests/test_skill_doc.py
git commit -m "feat: handoff skill with a test that its endpoints exist"
```

---

### Task 12: Deployment and README

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `handoff.service`, `README.md`
- Test: manual verification against a running instance

**Interfaces:**
- Consumes: `cli.main`.
- Produces: a runnable deployment.

- [ ] **Step 1: Write the `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 handoff && mkdir -p /data && chown handoff /data
USER handoff

ENV HANDOFF_DB=/data/handoff.db HANDOFF_BIND=0.0.0.0 HANDOFF_PORT=8080
VOLUME /data
EXPOSE 8080

ENTRYPOINT ["handoff"]
CMD ["serve"]
```

Note the deliberate tension: `serve` refuses `0.0.0.0`, and inside a container binding the
container's own interfaces is correct because the host publishes it selectively. Resolve it
by making the refusal skippable *only* via an explicit `--allow-any-interface` flag, and
have the Dockerfile pass it. Add to `build_parser`'s `serve` subparser:

```python
    serve.add_argument(
        "--allow-any-interface",
        action="store_true",
        help="permit binding 0.0.0.0 (containers only, where the host publishes the port)",
    )
```

Then thread the flag through, three edits in `cli.py`:

1. `_serve` gains a parameter: `def _serve(db_path, ttl_days, bind, port, conn, allow_any: bool) -> int:`
2. Its guard becomes `if bind == "0.0.0.0" and not allow_any:  # noqa: S104`
3. The call site becomes
   `return _serve(args.db, args.ttl_days, args.bind, args.port, conn, args.allow_any_interface)`

`test_bind_to_all_interfaces_is_refused` passes unchanged — it never sets the flag. Add:

```python
def test_bind_to_all_interfaces_allowed_with_explicit_flag():
    args = cli.build_parser().parse_args(["serve", "--bind", "0.0.0.0",
                                          "--allow-any-interface"])
    assert args.allow_any_interface is True
```

Then set the Dockerfile `CMD` to `["serve", "--allow-any-interface"]`.

- [ ] **Step 2: Write `.dockerignore`**

```
.git
.venv
tests
docs
*.db
__pycache__
```

- [ ] **Step 3: Write `handoff.service`**

Replace `TAILSCALE_IP` with the machine's tailnet address before installing.

```ini
[Unit]
Description=Agent hand-off site
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=handoff
Group=handoff
Environment=HANDOFF_DB=/var/lib/handoff/handoff.db
Environment=HANDOFF_TTL_DAYS=7
ExecStart=/opt/handoff/.venv/bin/handoff serve --bind TAILSCALE_IP --port 8080
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=/var/lib/handoff
StateDirectory=handoff

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Write `README.md`**

```markdown
# handoff

A hand-off board for AI agents working across machines, and for the human they work with.
Agents POST notes as JSON; the human reads them as rendered HTML. Everything expires seven
days after the last post.

This is a hand-off buffer, not a memory store. Anything durable goes in the repo.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/handoff --db handoff.db createuser yoshi
.venv/bin/handoff --db handoff.db serve --bind 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`, log in, go to `/agents`, and mint one token per machine.
The token is shown once.

## Agent setup

On each machine, set `HANDOFF_URL` and `HANDOFF_TOKEN`, and install the skill:

```bash
cp -r skills/handoff ~/.claude/skills/
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `HANDOFF_DB` | `handoff.db` | SQLite file |
| `HANDOFF_BIND` | `127.0.0.1` | Bind address. `0.0.0.0` is refused without `--allow-any-interface` |
| `HANDOFF_PORT` | `8080` | Port |
| `HANDOFF_TTL_DAYS` | `7` | Sliding expiry window |

## Security posture

- Designed for a trusted network (LAN or tailnet). Do not expose it to the internet
  without putting an authenticating proxy in front.
- Agent-supplied Markdown and HTML pass through one sanitizer before storage, and pages
  send `script-src 'none'` so a sanitizer bypass still executes nothing.
- There is no site JavaScript, deliberately. Do not add any.
- Agent tokens are stored as SHA-256 digests and shown exactly once.
- There is no delete endpoint. Expiry is the only removal path.

## Development

```bash
.venv/bin/pytest -v
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
```

- [ ] **Step 5: Apply the CLI change from Step 1 and run the full suite**

Run: `.venv/bin/pytest -v && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all green, including the new `test_bind_to_all_interfaces_allowed_with_explicit_flag`.

- [ ] **Step 6: Verify end to end against a real running instance**

```bash
rm -f /tmp/handoff-smoke.db
.venv/bin/handoff --db /tmp/handoff-smoke.db createuser smoketest
.venv/bin/handoff --db /tmp/handoff-smoke.db serve --port 8099 &
```

Then in a browser: log in, mint a token, and from a shell post to a folder with that token
and confirm the note renders on `/f/<slug>`. Confirm the page source contains no `<script>`.
Stop the server with `kill %1`.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile .dockerignore handoff.service README.md src/handoff/cli.py tests/test_cli.py
git commit -m "feat: container, systemd unit, and README"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: data model → 1; rendering and
sanitization → 2; authentication → 3 (primitives), 6 (session flow), 9 (minting); API →
5 (agent), 6–9 (human); expiry → 1 (reaper), 4 (sliding window and read filter), 7
(displayed countdown); the Skill → 11; testing → distributed across every task; deployment
→ 12. The spec's four "Deferred" items are implemented nowhere, as intended.

**Naming consistency.** `render.render`, `store.add_post`, `auth.agent_by_token`,
`db.reap`, `clock.now` are used with identical signatures wherever they appear across
tasks. `store.add_post` takes `ttl_days` as a keyword in every call site. Both the API and
the web layer read `request.app.state.ttl_days` rather than the environment.

**Two known rough edges, flagged rather than hidden:**

1. Task 5's `security_headers` middleware is written as `def` in the snippet but must be
   `async def` with `await call_next(request)`. The step text says so; do not copy the
   snippet verbatim.
2. Task 12 introduces `--allow-any-interface`, which modifies code written in Task 10.
   That is a real dependency between tasks, not an oversight — the container needs it and
   nothing before Task 12 does.
