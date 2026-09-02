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
    """Delete folders whose sliding TTL has run out, and expired sessions along with
    them. Safe to call any number of times. Returns the folder count only -- that's
    the number the CLI and callers already key off of; sessions are cleaned up as a
    side effect, not reported."""
    now = clock.now()
    cur = conn.execute("DELETE FROM folders WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    conn.commit()
    if cur.rowcount:
        # SQLite 3.46 (what CI's python:3.11 image ships) frees exactly one page per
        # `incremental_vacuum` call; 3.53 drains the whole freelist in one. Loop until
        # it stops shrinking, so bounded disk use holds on both.
        prev = None
        while (free := conn.execute("PRAGMA freelist_count").fetchone()[0]) and free != prev:
            prev = free
            conn.execute("PRAGMA incremental_vacuum").fetchall()
        conn.commit()
    return cur.rowcount
