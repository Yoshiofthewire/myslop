import sqlite3

import pytest

from handoff import auth, clock, db, store

DAY = 86400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * (50 * 1024)


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


def test_reap_fully_drains_the_freelist(conn, monkeypatch):
    """A single-step `PRAGMA incremental_vacuum` only frees one page per call --
    the file grows monotonically minus one page per reap. reap() must drain the
    freelist completely, or the store's own stated purpose (bounded disk use) is
    false in practice."""
    t = [1000]
    monkeypatch.setattr(clock, "now", lambda: t[0])
    for i in range(20):
        slug = f"s{i}"
        store.create_folder(conn, slug, "T", 7)
        store.add_post(
            conn, slug, "a", "agent", "T", "md", "x", images=[("i.png", PNG)], ttl_days=7
        )

    t[0] += 8 * DAY
    assert db.reap(conn) == 20
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_reap_deletes_expired_sessions_but_still_reports_folder_count(conn, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: 1000)
    uid = auth.create_user(conn, "yoshi", "hunter2")
    live_sid = auth.create_session(conn, uid)
    expired_sid = auth.create_session(conn, uid)
    conn.execute("UPDATE sessions SET expires_at = 999 WHERE id = ?", (expired_sid,))
    conn.commit()
    _folder(conn, "old", 999)

    assert db.reap(conn) == 1  # return value still counts folders only
    remaining = {r["id"] for r in conn.execute("SELECT id FROM sessions")}
    assert remaining == {live_sid}
